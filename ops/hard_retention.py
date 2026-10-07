#!/usr/bin/env python3
"""The execution half of the global 13-calendar-month retention ceiling.

`ops/retention_registry.py` says what may be kept and for how long. This module
is what actually removes it, for every governed store whose cleanup job names
`ops.hard_retention`. Configuration without an execution path is not
governance, and before this module most of the stores it sweeps had no cleanup
path at all — they simply grew.

WHAT IT DOES NOT DO. It is not a second prune. Stores that already had a
working, shorter lifecycle keep it and are absent from the sweep list below:
`public.runs`/`logs`/`artifacts` (60-day `api/platform_prune.py`),
`workflow_a_control.provider_request_log` (180 days, same module), Database
Explorer export objects (3 days), backup sets (14 days,
`ops/backup_retention.py`), Eco capability bearers (10/60 days, the host
ledger). This module only reaches what those do not.

THE SAFETY PROPERTIES, AND WHY EACH ONE IS THERE.

  DRY RUN BY DEFAULT. `--execute` is required to delete anything. A dry run
  plans and counts through exactly the same predicates.

  BOUNDED. Every delete is `... WHERE ctid IN (SELECT ctid ... LIMIT n FOR
  UPDATE SKIP LOCKED)` with a COMMIT per batch. A sweep of a seven-million-row
  table cannot become one enormous transaction, and an interrupted sweep leaves
  committed progress rather than a rolled-back night.

  IDEMPOTENT AND RESTART-SAFE. Every decision is a pure function of the cutoff
  and the row's own timestamp. Re-running removes a subset of the same set;
  interrupting and resuming is indistinguishable from never stopping.

  CONCURRENCY-SAFE. A session advisory lock on the platform database makes two
  sweeps mutually exclusive. Each batch is a single statement under one
  snapshot, so a `ctid` cannot be chosen and then reused by another row before
  the delete reaches it, and `SET LOCAL lock_timeout` bounds the contended case
  rather than waiting on it. There is deliberately no row-locking clause: it
  would require the UPDATE privilege, and a retention role has no business
  being able to rewrite customer rows.

  FAIL-CLOSED PER STORE, NOT PER RUN. A store whose relation is missing, whose
  anchor column is absent, or whose connection fails is recorded as failed and
  SKIPPED — it never causes a different store's eligible data to be retained.
  That is the "one malformed record must not cause unbounded retention of the
  dataset" requirement, applied at the granularity where it matters.

  IT REFUSES TO GUESS AN ANCHOR. Rows whose anchor timestamp is NULL are
  neither deleted nor ignored: they are counted as `unanchored`, reported, and
  the run is classified a partial failure. Deleting a row whose age cannot be
  established is worse than keeping it; silently keeping it is worse than
  saying so.

  IT REFUSES A BLOCKED POLICY. A registry entry with
  `Status.BLOCKED_OWNER_DECISION` is never executed, whatever the flags say.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.retention_registry import (  # noqa: E402
    GLOBAL_POLICY_ID,
    HARD_RETENTION_MONTHS,
    BY_ID as POLICY_BY_ID,
    PLATFORM_BACKUP_SET,
    Status,
    hard_retention_cutoff,
)

RESULT_SCHEMA = "log-platform-hard-retention/v1"
LOCK_NAMESPACE = "log-platform-hard-retention"
DEFAULT_BATCH_SIZE = 5000

#: Identifier grammar every schema/table/column in this module must satisfy.
#: The names are repository constants, never database- or operator-supplied, and
#: they are additionally passed through `psycopg.sql.Identifier`. This regex is
#: the third wall, and the cheapest one to keep.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HardRetentionError(RuntimeError):
    """A condition that must stop the whole sweep rather than one store."""


def cutoff_for(policy_id: str, now: datetime) -> datetime:
    """THE cutoff this store must be swept with, and why it is not one number.

    A single global cutoff was the defect: `now - 13 months` deletes at the
    deadline at the earliest, so a weekly sweep leaves records alive for up to
    another week past it, and any backup taken before the deletion keeps a copy
    for two weeks more. Both are answered by deleting EARLY, by a lead the
    registry derives from the responsible maintenance cycle and the audited
    backup topology — which is why the cutoff differs per store rather than
    being a constant this module owns.

    An unregistered policy id falls back to the bare deadline. That is
    deliberately the LEAST aggressive answer: an unknown store must never be
    swept harder than the policy allows, and `validate()` already fails on a
    sweep whose policy is missing.
    """
    policy = POLICY_BY_ID.get(policy_id)
    if policy is None:
        return hard_retention_cutoff(now)
    resolved = policy.enforcement_cutoff(now)
    return resolved if resolved is not None else hard_retention_cutoff(now)


# ---------------------------------------------------------------------------
# Sweep declarations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableSweep:
    """One relation, one anchor column, one optional protective predicate."""

    policy_id: str
    schema: str
    table: str
    anchor: str
    #: SQL that is TRUE for rows which are old enough but must NOT be deleted.
    #: Written against the alias `t`. Protected rows are counted and reported,
    #: never removed, and never treated as "done".
    protect_sql: str | None = None
    #: A relation the deployment may legitimately not have (an unapplied
    #: migration, a client whose schema predates a feature). Absent optional
    #: relations are reported as `absent`, not as failures.
    optional: bool = True
    #: Quoted mixed-case relation, e.g. `"Alpha_GPS_Baza_LOG"`. Identifier
    #: quoting handles it; the flag exists so the regex above can be relaxed
    #: for exactly these and nothing else.
    allow_mixed_case: bool = False
    note: str = ""

    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    def validate(self) -> None:
        for name in (self.schema, self.anchor):
            if not _IDENTIFIER_RE.match(name):
                raise HardRetentionError(f"unsafe identifier {name!r} in {self.policy_id}")
        if not self.allow_mixed_case and not _IDENTIFIER_RE.match(self.table):
            raise HardRetentionError(f"unsafe identifier {self.table!r} in {self.policy_id}")


@dataclass(frozen=True)
class ExemptStore:
    """A store the owner has explicitly exempted from age-based retention.

    Declared, not swept. The distinction this type exists for is the operator's:
    a store that is simply absent from the sweep list is indistinguishable from
    one nobody thought about, and "no output" must never be how an exemption is
    expressed. An `ExemptStore` produces a first-class outcome saying the
    relation has NO age-based retention by owner decision — never "0 candidates",
    never "blocked", never "unknown anchor".

    It carries NO anchor and NO predicate, so there is nothing here from which a
    DELETE could be composed even by mistake, and the run never opens a cursor
    against the relation: the exemption is a policy fact, not an observation of
    customer rows.
    """

    policy_id: str
    schema: str
    table: str
    note: str = ""

    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


#: Client-business relations governed by an owner-approved exemption. Every one
#: must resolve to a `Mode.OWNER_EXEMPT` policy — `exempt_outcome()` refuses
#: otherwise, so this list cannot quietly grow into a way of skipping a sweep.
CLIENT_EXEMPT_STORES: tuple[ExemptStore, ...] = (
    ExemptStore(
        policy_id="client_db.workflow_b_gps_assignment_log",
        schema="telematics_reports", table="Alpha_GPS_Baza_LOG",
        note=(
            "OWNER DECISION 2026-08-29: the GPS assignment log has no age-based "
            "retention. Records are neither deleted nor classified as expired "
            "because of their age, and no anchor — semantic `assignment_date` "
            "or ingestion `imported_at` — is used to plan a deletion."
        ),
    ),
    ExemptStore(
        policy_id="client_db.workflow_b_gps_assignment_log",
        schema="telematics_reports", table="Alpha_GPS_Baza_LOG",
        note="The pre-rename twin of the exempted assignment log.",
    ),
)


@dataclass(frozen=True)
class FilesystemSweep:
    policy_id: str
    #: Absolute path, or an environment variable name plus a default.
    root: Path
    note: str = ""


# -- Platform database ------------------------------------------------------
#
# Ordered deliberately: a child that would otherwise block its parent's delete
# is swept first. `run_reconciliation` before `runs` (its FK is RESTRICT and is
# exactly what keeps a reconciled run alive past 60 days); occurrences before
# incidents; attempt objects before jobs; report files cascade from instances.

PLATFORM_SWEEPS: tuple[TableSweep, ...] = (
    TableSweep(
        policy_id="platform_db.ops_control.run_reconciliation",
        schema="ops_control", table="run_reconciliation", anchor="reconciled_at",
        note=(
            "Swept before anything else touching runs. Its ON DELETE RESTRICT is "
            "what makes a reconciled run unprunable; removing the evidence at the "
            "ceiling is what lets the ordinary 60-day pass finally claim the run."
        ),
    ),
    TableSweep(
        policy_id="platform_db.ops_control.watchdog_observation",
        schema="ops_control", table="watchdog_observation", anchor="last_observed_at",
        note="One row per subject, refreshed in place; only abandoned subjects age out.",
    ),
    TableSweep(
        policy_id="platform_db.ops_control.environment_identity_promotion",
        schema="ops_control", table="environment_identity_promotion", anchor="created_at",
    ),
    TableSweep(
        policy_id="platform_db.public.portal_audit_events",
        schema="public", table="portal_audit_events", anchor="created_at",
        optional=False,
    ),
    TableSweep(
        policy_id="platform_db.public.suspected_bug_occurrences",
        schema="public", table="suspected_bug_occurrences", anchor="occurred_at",
        note=(
            "Swept in its own right so a still-open, still-recurring incident "
            "cannot carry occurrences older than the ceiling on its back."
        ),
    ),
    TableSweep(
        policy_id="platform_db.public.suspected_bug_email_outbox",
        schema="public", table="suspected_bug_email_outbox", anchor="created_at",
        protect_sql="t.status IN ('pending', 'sending')",
        note="An undelivered alert is work in progress, not history.",
    ),
    TableSweep(
        policy_id="platform_db.public.suspected_bug_incidents",
        schema="public", table="suspected_bug_incidents", anchor="last_seen_at",
        note=(
            "Anchored on LAST seen. A fingerprint that is still firing keeps "
            "suppressing duplicate alerts however old it is; only one silent for "
            "the whole ceiling is removed, cascading its occurrences and outbox."
        ),
    ),
    TableSweep(
        policy_id="platform_db.public.database_export_attempt_objects",
        schema="public", table="database_export_attempt_objects", anchor="created_at",
        protect_sql="t.state = 'cleanup_pending' AND t.last_cleanup_success_at IS NULL",
        note=(
            "A cleanup_pending row NAMES an unpublished MinIO object nothing else "
            "records. Deleting the row would orphan the object forever, so the "
            "ledger row outlives the ceiling until its object is proven gone."
        ),
    ),
    TableSweep(
        policy_id="platform_db.public.database_export_jobs",
        schema="public", table="database_export_jobs", anchor="created_at",
        protect_sql=(
            "t.status IN ('queued', 'running') "
            "OR EXISTS (SELECT 1 FROM public.database_export_attempt_objects o "
            "            WHERE o.job_id = t.job_id "
            "              AND o.state = 'cleanup_pending' "
            "              AND o.last_cleanup_success_at IS NULL)"
        ),
        note=(
            "Cascades its attempt-object ledger, so it must not be deleted while "
            "one of those rows is still the only record of an unpublished object."
        ),
    ),
    TableSweep(
        policy_id="platform_db.public.portal_generated_report_instances",
        schema="public", table="portal_generated_report_instances", anchor="created_at",
        protect_sql=(
            "t.generation_state IN ('pending', 'running') "
            "OR EXISTS (SELECT 1 FROM public.portal_generated_report_files f "
            "            WHERE f.instance_id = t.instance_id AND f.is_available)"
        ),
        note=(
            "Member rows cascade. An instance still offering a downloadable file "
            "is protected: withdrawing availability is the publisher's job, not "
            "retention's."
        ),
    ),
    TableSweep(
        policy_id="platform_db.workflow_a_control.client_schedule_run_history",
        schema="workflow_a_control", table="client_schedule_run_history",
        anchor="scheduled_fire_ts",
        protect_sql="t.status = 'RUNNING'",
        note=(
            "The dispatcher's single-job gate counts RUNNING rows, so one is live "
            "state however old its scheduled fire is. Deleting a fire cascades "
            "the provider_request_log rows bound to it, which are older still."
        ),
    ),
    TableSweep(
        policy_id="platform_db.workflow_a_control.client_dataset_recovery_run",
        schema="workflow_a_control", table="client_dataset_recovery_run", anchor="created_at",
    ),
    TableSweep(
        policy_id="platform_db.workflow_a_control.trip_delivery_lag_daily",
        schema="workflow_a_control", table="trip_delivery_lag_daily", anchor="trip_end_date",
    ),
    TableSweep(
        policy_id="platform_db.ingest.imap_message",
        schema="ingest", table="imap_message", anchor="fetched_at",
        protect_sql=(
            "EXISTS (SELECT 1 FROM ingest.raw_file mine "
            "         JOIN ingest.raw_file other ON other.duplicate_of_id = mine.id "
            "        WHERE mine.imap_message_id = t.id "
            "          AND other.imap_message_id <> t.id)"
        ),
        note=(
            "raw_file cascades from here — it has no timestamp of its own, so the "
            "message IS the anchor for the whole record family. "
            "`raw_file.duplicate_of_id` is NO ACTION, so a message whose files a "
            "YOUNGER message still points at is deferred rather than allowed to "
            "abort the batch. It becomes eligible once the pointer ages out too."
        ),
    ),
)


# -- Client business databases ---------------------------------------------
#
# `client_db.workflow_a_registered_tables` is expanded from
# `jobs.api.telematics.registry.TABLES` rather than transcribed, so a table added
# there is governed the same day. The rest are the relations that registry does
# not know about — which, before this module, meant nothing governed them.

CLIENT_EXTRA_SWEEPS: tuple[TableSweep, ...] = (
    TableSweep(
        policy_id="client_db.eco_driving_email_send_log",
        schema="public", table="eco_driving_weekly_email_send_log", anchor="attempted_at",
    ),
    TableSweep(
        policy_id="client_db.eco_driving_email_send_log",
        schema="public", table="eco_driving_monthly_email_send_log", anchor="attempted_at",
    ),
    TableSweep(
        policy_id="client_db.eco_drivers_id_chart",
        schema="public", table="eco_drivers_id_chart", anchor="updated_at",
    ),
    TableSweep(
        policy_id="client_db.eco_dashboard_delivery_operation",
        schema="public", table="eco_dashboard_delivery_operation", anchor="created_at",
        protect_sql="t.capability_secret IS NOT NULL OR t.operator_action_required",
        note=(
            "A row still holding a raw bearer cannot legitimately be 13 months "
            "old — the longest grant lives 60 days — so this predicate is a net "
            "under the secret-minimisation sweep rather than a routine exclusion. "
            "If it ever protects a row, retirement is not running."
        ),
    ),
    TableSweep(
        policy_id="client_db.workflow_b_stage3_report_tables",
        schema="telematics_reports", table="report_207", anchor="_loaded_at",
    ),
    TableSweep(
        policy_id="client_db.workflow_b_stage3_report_tables",
        schema="telematics_reports", table="report_d105_2_ecodriving", anchor="_loaded_at",
    ),
    TableSweep(
        policy_id="client_db.legacy_backup_tables",
        schema="public", table="client_trips_legacy_backup_020", anchor="start_timestamp",
    ),
    TableSweep(
        policy_id="client_db.legacy_backup_tables",
        schema="public", table="client_trips_legacy_backup_021", anchor="start_timestamp",
    ),
    # The 2026-06-19 D105.2 write-test snapshots in alpha_main. No DDL in this
    # repository creates them — an operator did, before a write test — so they
    # were invisible to the migration-parsing coverage check and only a live
    # `--coverage --clients` census found them.
    #
    # TWO NAMES, NOT A PATTERN. Each carries the anchor of the table it was
    # copied from, which is why they fit `legacy_backup_tables` rather than
    # needing a policy of their own: `start_timestamp` for the trip copy,
    # `_loaded_at` for the report copy. A `backup_*` wildcard over
    # `telematics_reports` would also claim operator and forensic relations, and a
    # deletion rule nobody wrote is exactly what this module refuses to be.
    # `optional=True` (the default) means the four clients that do not have them
    # report `RELATION_ABSENT` rather than failing.
    TableSweep(
        policy_id="client_db.legacy_backup_tables",
        schema="telematics_reports",
        table="backup_client_trips_d105_2_write_test_20260619_100539",
        anchor="start_timestamp",
        note=(
            "Pre-write snapshot of public.client_trips, kept by the rollback "
            "script ops/reports/d105_2_ecodriving_alpha00001_local_write_test_"
            "rollback_20260619_100539.sql. Anchored on the trip's own start, "
            "exactly like the migration 020/021 copies."
        ),
    ),
    TableSweep(
        policy_id="client_db.legacy_backup_tables",
        schema="telematics_reports",
        table="backup_d105_2_ecodriving_write_test_20260619_100539",
        anchor="_loaded_at",
        note=(
            "Pre-write snapshot of telematics_reports.report_d105_2_ecodriving, "
            "anchored on the same Stage 3 load timestamp the live report table "
            "is swept by."
        ),
    ),
    # NOTE: `telematics_reports."Alpha_GPS_Baza_LOG"` and its pre-rename twin are
    # deliberately ABSENT from this tuple. They are the platform's one
    # owner-approved exemption from age-based retention, declared as
    # `Mode.OWNER_EXEMPT` in the registry, and they are reported explicitly by
    # `EXEMPT_STORES` below rather than swept. Adding a TableSweep for them
    # would plan DELETEs the owner has forbidden.
    TableSweep(
        policy_id="client_db.workflow_b_gps_assignment_import_runs",
        schema="telematics_reports", table="alpha_gps_baza_log_import_runs",
        anchor="started_at",
        note=(
            "Import-run history for the assignment log — operational provenance, "
            "not the exempted business history. Ages out on its own start under "
            "the ordinary ceiling."
        ),
    ),
    TableSweep(
        policy_id="client_db.v2_staging_tables",
        schema="public", table="source_trips", anchor="synced_at",
    ),
    TableSweep(
        policy_id="client_db.v2_staging_tables",
        schema="public", table="source_notifications", anchor="synced_at",
    ),
    TableSweep(
        policy_id="client_db.v2_staging_tables",
        schema="public", table="source_fuel_observations", anchor="synced_at",
    ),
)


def workflow_a_table_sweeps() -> tuple[TableSweep, ...]:
    """Expand `registry.TABLES` into sweeps, so the two can never disagree."""
    from jobs.api.telematics import registry  # noqa: PLC0415 - optional at import time

    return tuple(
        TableSweep(
            policy_id="client_db.workflow_a_registered_tables",
            schema=spec.schema,
            table=spec.name,
            anchor=spec.retention_key_column,
        )
        for spec in sorted(registry.TABLES.values(), key=lambda item: item.name)
    )


# -- Filesystem -------------------------------------------------------------

def _reports_data_dir() -> Path:
    return Path(
        os.getenv("REPORTS_DATA_DIR", "/home/logplatform/data/reports")
    )


def filesystem_sweeps() -> tuple[FilesystemSweep, ...]:
    base = _reports_data_dir()
    return (
        FilesystemSweep(
            policy_id="filesystem.workflow_b_report_files", root=base / "raw",
        ),
        FilesystemSweep(
            policy_id="filesystem.workflow_b_report_files", root=base / "normalized",
        ),
        FilesystemSweep(
            policy_id="filesystem.workflow_b_stage2_cleaned",
            root=Path("/tmp/log-platform-stage2/cleaned"),
        ),
    )


#: Directories a recursive deleter may never be pointed at, however it was
#: configured. `REPORTS_DATA_DIR` is operator configuration, and configuration
#: that can select the target of an `unlink()` walk needs a floor under it.
_FORBIDDEN_ROOTS: frozenset[str] = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys",
    "/tmp", "/usr", "/var",
})

#: The stage-2 scratch root is a repository constant; nothing configures it.
_STAGE2_ROOT = Path("/tmp/log-platform-stage2")


class UnsafeRoot(RuntimeError):
    pass


def _structurally_safe(candidate: Path) -> Path:
    """Prove a path is deep enough, real, and not a system directory."""
    if not candidate.is_absolute():
        raise UnsafeRoot(f"{candidate} is not an absolute path")
    resolved = candidate.resolve()
    if str(resolved) in _FORBIDDEN_ROOTS or len(resolved.parts) < 3:
        raise UnsafeRoot(f"{resolved} is a system directory or too close to /")
    if resolved == Path.home().resolve():
        raise UnsafeRoot(f"{resolved} is the account home directory")
    return resolved


def approved_filesystem_prefixes() -> tuple[Path, ...]:
    """The roots a sweep may descend, derived once and structurally proven.

    `REPORTS_DATA_DIR` genuinely differs between the repository default and the
    production host (`/home/logplatform/reports-data`), so hard-coding
    one path made the sweep silently refuse the real directory. The configured
    value is therefore accepted — but only after `_structurally_safe` proves it
    is not `/`, not a system directory, and not the account home, so a typo or a
    tampered environment cannot widen the walk.
    """
    prefixes: list[Path] = []
    for candidate in (_reports_data_dir(), _STAGE2_ROOT):
        try:
            prefixes.append(_structurally_safe(candidate))
        except UnsafeRoot:
            continue
    return tuple(prefixes)


def assert_safe_root(root: Path) -> Path:
    """Refuse anything that is not provably an approved, non-symlinked directory.

    The checks are ordered so that no property is established on a path a later
    check could reinterpret: absolute first, then resolved (which follows every
    symlink component), then the structural floor, then membership in the
    approved set, then a final proof that the resolved path is a real directory
    and not a symlink to one.
    """
    resolved = _structurally_safe(root)
    approved = approved_filesystem_prefixes()
    if not any(resolved == prefix or prefix in resolved.parents for prefix in approved):
        raise UnsafeRoot(f"{resolved} is outside every approved retention root")
    if resolved.is_symlink():
        raise UnsafeRoot(f"{resolved} is a symlink, not a real directory")
    if not resolved.exists():
        # A governed root that has not been created yet — the stage-2 scratch
        # directory before Stage 2 has ever run — is ABSENT, not unsafe. The
        # distinction matters: "unsafe" is an operator defect and shows up in
        # `defects`, while "absent" is the ordinary state of an empty pipeline.
        raise FileNotFoundError(str(resolved))
    if not resolved.is_dir():
        raise UnsafeRoot(f"{resolved} is not a directory")
    return resolved


# ---------------------------------------------------------------------------
# Outcome records
# ---------------------------------------------------------------------------

def _lead_seconds(policy_id: str) -> int:
    policy = POLICY_BY_ID.get(policy_id)
    return int(policy.enforcement_lead().total_seconds()) if policy else 0


#: The classification an owner-exempt store reports. Deliberately NOT one of
#: the five execution classifications in
#: `db/migrations/070_platform_retention_execution_ledger.sql`: an exemption is
#: not a sweep outcome, nothing was examined and nothing could have been
#: deleted, so it is reported to the operator and left out of the execution
#: ledger rather than recorded there as if a pass had run.
EXEMPT_CLASSIFICATION = "RETENTION_EXEMPT_NO_AGE_RETENTION"


@dataclass
class SweepOutcome:
    policy_id: str
    scope: str
    store: str
    #: `None` only for an exempt store, which has no cutoff to report because
    #: its policy defines none.
    cutoff: datetime | None
    dry_run: bool
    classification: str = "RETENTION_DRY_RUN_SUCCEEDED"
    examined: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0
    unanchored: int = 0
    batches: int = 0
    oldest_remaining: datetime | None = None
    error: str | None = None
    #: Machine-readable reason a sweep did not fully govern its store. This is
    #: what an operator dashboard filters on; `error` is the human sentence.
    defect_code: str | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "scope": self.scope,
            "store": self.store,
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
            "enforcement_lead_seconds": _lead_seconds(self.policy_id),
            "dry_run": self.dry_run,
            "classification": self.classification,
            "examined_count": self.examined,
            "deleted_count": self.deleted,
            "skipped_count": self.skipped,
            "failed_count": self.failed,
            "unanchored_count": self.unanchored,
            "batches": self.batches,
            "oldest_remaining_ts": (
                self.oldest_remaining.isoformat() if self.oldest_remaining else None
            ),
            "error": self.error,
            "defect_code": self.defect_code,
            "note": self.note or None,
        }


def exempt_outcome(store: "ExemptStore", *, scope: str, dry_run: bool) -> "SweepOutcome":
    """Report an owner-exempt relation. Touches no database and plans no DELETE.

    Fails closed on the registry rather than on this module's own list: if the
    named policy is not `Mode.OWNER_EXEMPT`, the store is reported as a defect
    instead of being silently skipped. That is what keeps this tuple from ever
    becoming a back door around a governed sweep.
    """
    outcome = SweepOutcome(
        policy_id=store.policy_id, scope=scope, store=store.qualified(),
        cutoff=None, dry_run=dry_run,
        classification=EXEMPT_CLASSIFICATION, note=store.note,
    )
    policy = POLICY_BY_ID.get(store.policy_id)
    if policy is None or not getattr(policy, "is_owner_exempt", False):
        outcome.classification = "RETENTION_FAILED"
        outcome.failed = 1
        outcome.defect_code = "EXEMPTION_NOT_REGISTERED"
        outcome.error = (
            f"{store.qualified()} is declared exempt here but "
            f"{store.policy_id!r} is not an owner-approved exemption in the registry"
        )
    return outcome


# ---------------------------------------------------------------------------
# PostgreSQL sweep
# ---------------------------------------------------------------------------

def _sql():
    from psycopg import sql  # noqa: PLC0415

    return sql


def relation_exists(cur, sweep: TableSweep) -> bool:
    # `quote_ident` rather than a client-side f-string: the names are repository
    # constants, but the quoting rule for a mixed-case relation like
    # `"Alpha_GPS_Baza_LOG"` belongs to the server, not to this module.
    cur.execute(
        "SELECT to_regclass(quote_ident(%s) || '.' || quote_ident(%s)) IS NOT NULL"
        " AS present",
        (sweep.schema, sweep.table),
    )
    row = cur.fetchone()
    return bool(row[0] if isinstance(row, (tuple, list)) else row["present"])


def anchor_column_exists(cur, sweep: TableSweep) -> bool:
    """Ask the catalog, not `information_schema`.

    `information_schema.columns` is PRIVILEGE-FILTERED: it hides every column
    the connected role holds no privilege on. Connected as a per-client business
    role, that made a dozen perfectly present anchor columns read as missing and
    fail their sweeps closed — a silent, total loss of retention coverage that
    looked like schema drift. `pg_attribute` reports what actually exists.
    """
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s
               AND a.attnum > 0 AND NOT a.attisdropped
        ) AS present
        """,
        (sweep.schema, sweep.table, sweep.anchor),
    )
    row = cur.fetchone()
    return bool(row[0] if isinstance(row, (tuple, list)) else row["present"])


def _scalar(cur) -> Any:
    row = cur.fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row, (tuple, list)) else next(iter(row.values()))


def sweep_table(
    conn,
    sweep: TableSweep,
    *,
    cutoff: datetime,
    scope: str,
    dry_run: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = None,
) -> SweepOutcome:
    """Sweep one relation. Never raises for a per-store problem — records it."""
    sweep.validate()
    sql = _sql()
    outcome = SweepOutcome(
        policy_id=sweep.policy_id, scope=scope, store=sweep.qualified(),
        cutoff=cutoff, dry_run=dry_run, note=sweep.note,
    )

    policy = POLICY_BY_ID.get(sweep.policy_id)
    if policy is not None and policy.status is Status.BLOCKED_OWNER_DECISION:
        outcome.classification = "RETENTION_SKIPPED_BLOCKED"
        outcome.defect_code = "BLOCKED_POLICY"
        outcome.error = "policy is blocked pending an owner decision"
        return outcome

    relation = sql.SQL("{}.{}").format(
        sql.Identifier(sweep.schema), sql.Identifier(sweep.table)
    )
    anchor = sql.Identifier(sweep.anchor)
    protect = (
        sql.SQL("(") + sql.SQL(sweep.protect_sql) + sql.SQL(")")
        if sweep.protect_sql else sql.SQL("false")
    )

    try:
        with conn.cursor() as cur:
            if not relation_exists(cur, sweep):
                conn.rollback()
                outcome.classification = (
                    "RETENTION_DRY_RUN_SUCCEEDED" if sweep.optional
                    else "RETENTION_FAILED"
                )
                outcome.error = "relation absent"
                outcome.defect_code = "RELATION_ABSENT"
                if not sweep.optional:
                    outcome.failed = 1
                return outcome
            if not anchor_column_exists(cur, sweep):
                conn.rollback()
                outcome.classification = "RETENTION_FAILED"
                outcome.failed = 1
                outcome.error = f"anchor column {sweep.anchor!r} is absent"
                outcome.defect_code = "ANCHOR_COLUMN_ABSENT"
                return outcome

            # Census first, from one consistent read: how many are old enough,
            # how many of those are protected, and how many rows can never be
            # aged at all because their anchor is NULL.
            cur.execute(
                sql.SQL(
                    "SELECT count(*) FILTER (WHERE t.{anchor} < %s) AS eligible, "
                    "       count(*) FILTER (WHERE t.{anchor} < %s AND {protect}) AS protected, "
                    "       count(*) FILTER (WHERE t.{anchor} IS NULL) AS unanchored "
                    "  FROM {relation} t"
                ).format(anchor=anchor, protect=protect, relation=relation),
                (cutoff, cutoff),
            )
            row = cur.fetchone()
            values = list(row) if isinstance(row, (tuple, list)) else list(row.values())
            eligible, protected, unanchored = (int(value) for value in values[:3])
            conn.rollback()
    except Exception as exc:  # per-store fail-closed
        _safe_rollback(conn)
        outcome.classification = "RETENTION_FAILED"
        outcome.failed = 1
        outcome.error = f"{type(exc).__name__}: {exc}"[:400]
        # A missing GRANT is not a bug in this module and not a transient
        # fault: it is an operator action, and it must be nameable as one.
        outcome.defect_code = (
            "INSUFFICIENT_PRIVILEGE"
            if type(exc).__name__ == "InsufficientPrivilege"
            else "STORE_QUERY_FAILED"
        )
        return outcome

    outcome.examined = eligible
    outcome.skipped = protected
    outcome.unanchored = unanchored
    if unanchored:
        outcome.defect_code = "UNANCHORED_ROWS"

    if dry_run:
        outcome.classification = (
            "RETENTION_PARTIAL_FAILURE" if unanchored else "RETENTION_DRY_RUN_SUCCEEDED"
        )
        outcome.oldest_remaining = _oldest_remaining(
            conn, sweep, cutoff=cutoff, relation=relation, anchor=anchor,
        )
        return outcome

    deletable = eligible - protected
    if deletable > 0:
        # ONE STATEMENT, AND DELIBERATELY NO LOCKING CLAUSE.
        #
        # `FOR UPDATE SKIP LOCKED` would let a contended row be deferred to the
        # next batch instead of waiting for it — but PostgreSQL requires the
        # UPDATE privilege for any row-locking clause, and granting a RETENTION
        # role UPDATE on customer tables would let it rewrite business records,
        # not merely remove expired ones. Deletion is destructive; falsification
        # is worse, and it is not a privilege this job has any use for. The
        # `SELECT, DELETE` grant in
        # `db/client_business/052_retention_runtime_privileges.sql` is what the
        # supported operation actually needs, so the operation is written to
        # need exactly that.
        #
        # Nothing is lost on correctness. The subquery and the delete are ONE
        # statement, evaluated under one snapshot, so no `ctid` can be reclaimed
        # by VACUUM and reused by a different row between choosing and deleting
        # it — the hazard a locking clause would otherwise be guarding against.
        # What changes is only the contended case: with `SET LOCAL lock_timeout`
        # below, a batch blocked by another transaction fails after five seconds
        # and is reported as a partial failure, and the next run resumes from
        # committed progress. Rows old enough to be eligible here have not been
        # written to for thirteen months, so that case is close to unreachable.
        delete_sql = sql.SQL(
            "DELETE FROM {relation} "
            " WHERE ctid IN ("
            "   SELECT t.ctid FROM {relation} t "
            "    WHERE t.{anchor} < %s AND NOT {protect} "
            "    ORDER BY t.{anchor} "
            "    LIMIT %s"
            " )"
        ).format(relation=relation, anchor=anchor, protect=protect)
        try:
            while True:
                outcome.batches += 1
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '5s'")
                    cur.execute("SET LOCAL statement_timeout = '15min'")
                    cur.execute(delete_sql, (cutoff, batch_size))
                    removed = cur.rowcount or 0
                conn.commit()
                outcome.deleted += removed
                if removed < batch_size:
                    break
                if max_batches is not None and outcome.batches >= max_batches:
                    break
        except Exception as exc:
            _safe_rollback(conn)
            outcome.classification = "RETENTION_PARTIAL_FAILURE"
            outcome.failed = 1
            outcome.error = f"{type(exc).__name__}: {exc}"[:400]
            outcome.oldest_remaining = _oldest_remaining(
                conn, sweep, cutoff=cutoff, relation=relation, anchor=anchor,
            )
            return outcome

    outcome.oldest_remaining = _oldest_remaining(
        conn, sweep, cutoff=cutoff, relation=relation, anchor=anchor,
    )
    outcome.classification = (
        "RETENTION_PARTIAL_FAILURE" if unanchored else "RETENTION_EXECUTION_SUCCEEDED"
    )
    return outcome


def _oldest_remaining(conn, sweep: TableSweep, *, cutoff, relation, anchor) -> datetime | None:
    """The oldest still-eligible timestamp left behind. THE compliance number.

    "Deleted 0 rows" is ambiguous; this is not. A value here means something
    older than the cutoff survived — normally because it is protected, which is
    exactly the case an operator must be able to see growing.
    """
    sql = _sql()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT min(t.{anchor}) FROM {relation} t WHERE t.{anchor} < %s")
                .format(anchor=anchor, relation=relation),
                (cutoff,),
            )
            value = _scalar(cur)
        conn.rollback()
    except Exception:
        _safe_rollback(conn)
        return None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # A `date` anchor (trip_end_date, day, period_start_date) compares fine in
    # SQL but must become an instant before it can be recorded next to one.
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _safe_rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Filesystem sweep
# ---------------------------------------------------------------------------

def sweep_filesystem(
    sweep: FilesystemSweep, *, cutoff: datetime, dry_run: bool,
    max_files: int = 100_000,
) -> SweepOutcome:
    outcome = SweepOutcome(
        policy_id=sweep.policy_id, scope=str(sweep.root), store=str(sweep.root),
        cutoff=cutoff, dry_run=dry_run, note=sweep.note,
    )
    try:
        root = assert_safe_root(sweep.root)
    except UnsafeRoot as exc:
        outcome.classification = "RETENTION_FAILED"
        outcome.failed = 1
        outcome.error = str(exc)
        outcome.defect_code = "UNSAFE_ROOT"
        return outcome
    except FileNotFoundError:
        outcome.error = "root absent"
        outcome.defect_code = "RELATION_ABSENT"
        return outcome
    except OSError as exc:
        outcome.classification = "RETENTION_FAILED"
        outcome.failed = 1
        outcome.error = f"{type(exc).__name__}: {exc}"[:200]
        outcome.defect_code = "UNSAFE_ROOT"
        return outcome

    cutoff_epoch = cutoff.timestamp()
    oldest: float | None = None
    examined = 0
    deleted = 0
    failed = 0
    skipped = 0

    for path in _walk_regular_files(root, limit=max_files):
        try:
            info = path.lstat()
        except OSError:
            failed += 1
            continue
        if info.st_mtime >= cutoff_epoch:
            continue
        examined += 1
        # Re-prove containment on the RESOLVED path immediately before the
        # unlink. `_walk_regular_files` never descends a symlinked directory,
        # but a path can be replaced between the walk and the delete, and the
        # cheap check is the one that makes that race harmless.
        try:
            resolved = path.resolve()
            if root not in resolved.parents:
                skipped += 1
                continue
        except OSError:
            failed += 1
            continue
        if dry_run:
            if oldest is None or info.st_mtime < oldest:
                oldest = info.st_mtime
            continue
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            deleted += 1  # already gone: idempotent, not an error
        except OSError:
            failed += 1
            if oldest is None or info.st_mtime < oldest:
                oldest = info.st_mtime

    outcome.examined = examined
    outcome.deleted = deleted
    outcome.failed = failed
    outcome.skipped = skipped
    outcome.oldest_remaining = (
        datetime.fromtimestamp(oldest, tz=timezone.utc) if oldest is not None else None
    )
    if failed:
        outcome.classification = "RETENTION_PARTIAL_FAILURE"
    elif dry_run:
        outcome.classification = "RETENTION_DRY_RUN_SUCCEEDED"
    else:
        outcome.classification = "RETENTION_EXECUTION_SUCCEEDED"
    return outcome


def _walk_regular_files(root: Path, *, limit: int) -> Iterable[Path]:
    """Yield regular files under `root`, never following a symlinked directory."""
    seen = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if seen >= limit:
                return
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if entry.is_file(follow_symlinks=False):
                    seen += 1
                    yield Path(entry.path)
            except OSError:
                continue


# ---------------------------------------------------------------------------
# The artifact ceiling pass
# ---------------------------------------------------------------------------

ARTIFACT_CEILING_POLICY = "platform_db.public.artifacts_reference_excluded"


def sweep_artifact_ceiling(*, dry_run: bool, now: datetime) -> SweepOutcome:
    """Run `api/platform_prune.py`'s ceiling pass as one more governed sweep.

    WHY IT IS CALLED FROM HERE RATHER THAN GIVEN ITS OWN ExecStart. The pass has
    to happen on the same schedule as everything else, and a second `ExecStart=`
    line would couple the two: a MinIO outage in the artifact pass would abort
    the unit and the entire row sweep would never run. Calling it as a sweep
    gives it exactly the isolation every other store gets — its own try/except,
    its own defect code, its own ledger row — and keeps one operator command.

    WHY IT IS NOT REIMPLEMENTED HERE. `api/platform_prune.py` already attests
    the platform identity, validates the artifact reference contract against
    `pg_constraint`, locks the reviewed relations, runs SERIALIZABLE and deletes
    the MinIO object before its row. Writing a second artifact deleter with its
    own idea of what is safe is precisely what this module must not do.

    An environment without object-store configuration or without `boto3` is not
    a failure of this run: it is reported as unavailable, so an operator sees a
    store that was NOT swept instead of a silent absence.
    """
    outcome = SweepOutcome(
        policy_id=ARTIFACT_CEILING_POLICY, scope="platform",
        store="public.artifacts + MinIO objects past the ceiling",
        cutoff=cutoff_for(ARTIFACT_CEILING_POLICY, now), dry_run=dry_run,
        note=(
            "Same planner and same reference contract as the 60-day pass; the "
            "two retention-class exclusions stop protecting at the ceiling."
        ),
    )
    try:
        from api import platform_prune  # noqa: PLC0415
    except Exception as exc:
        outcome.classification = "RETENTION_FAILED"
        outcome.defect_code = "ARTIFACT_CEILING_UNAVAILABLE"
        outcome.error = f"platform_prune unavailable: {type(exc).__name__}"
        outcome.failed = 1
        return outcome

    try:
        identity = platform_prune.load_runtime_identity()
        bucket = os.environ["MINIO_BUCKET"]
        result = platform_prune.run_prune(
            connection_factory=platform_prune._connection_factory(identity),
            s3_client=platform_prune._s3_client(),
            bucket=bucket,
            identity=identity,
            retention_days=None,
            dry_run=dry_run,
            now_utc=now,
            hard_ceiling=True,
            ceiling_cutoff=cutoff_for(ARTIFACT_CEILING_POLICY, now),
        )
    except KeyError as exc:
        outcome.classification = "RETENTION_FAILED"
        outcome.defect_code = "ARTIFACT_CEILING_UNAVAILABLE"
        outcome.error = f"object store not configured: {exc}"
        outcome.failed = 1
        return outcome
    except Exception as exc:
        outcome.classification = "RETENTION_FAILED"
        outcome.defect_code = "ARTIFACT_CEILING_FAILED"
        outcome.error = f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"[:300]
        outcome.failed = 1
        return outcome

    plan = result.get("plan", {})
    mutations = result.get("mutations", {})
    candidates = plan.get("candidates", {})
    outcome.examined = int(candidates.get("artifact_rows", 0))
    outcome.deleted = int(mutations.get("artifact_rows_deleted", 0))
    outcome.skipped = sum(int(value) for value in (plan.get("excluded") or {}).values())
    outcome.classification = (
        "RETENTION_DRY_RUN_SUCCEEDED" if dry_run else "RETENTION_EXECUTION_SUCCEEDED"
    )
    return outcome


# ---------------------------------------------------------------------------
# The Cloudflare D1 / R2 maintenance pass
# ---------------------------------------------------------------------------

ECO_MAINTENANCE_POLICIES = (
    "cloudflare_d1.eco_session",
    "cloudflare_d1.eco_capability",
    "cloudflare_d1.eco_publication_operation",
    "cloudflare_r2.driver_eco_snapshots",
)

ENV_PUBLISHER_ENDPOINT = "ECO_DASHBOARD_PUBLISHER_URL"
ENV_PUBLISHER_TOKEN = "ECO_DASHBOARD_PUBLISHER_TOKEN"


def sweep_eco_dashboard_maintenance(*, dry_run: bool, now: datetime) -> list[SweepOutcome]:
    """Drive the Worker's D1/R2 hard retention from the PLATFORM schedule.

    WHY THIS EXISTS, AND WHY IT IS NOT OPTIONAL. The Worker's retention already
    worked — but the only caller was the Eco mailing run's maintenance boundary,
    which means D1 grants, the publication ledger and R2 snapshots were governed
    only for as long as somebody kept sending Eco e-mail. In production four of
    five clients have every Eco schedule disabled, so business activity is not a
    retention scheduler and must never be treated as one. This pass gives the
    Cloudflare stores the same weekly guarantee every other store has,
    independent of publication traffic, driver activity, capability minting and
    operator memory.

    WHAT IT DOES NOT DO. It introduces no new route, no cron trigger and no
    unauthenticated surface: it is the same publisher-authenticated
    `POST /api/publish/maintenance` the mailing run calls, over the same
    validated endpoint and the same machine credential. If the credential or the
    endpoint is absent this is reported as unavailable — the stores are then
    visibly ungoverned rather than silently so.

    A dry run deliberately does NOT call the route. The endpoint has no
    plan-only mode, so the honest dry-run answer is "this is what would be
    contacted", not a mutation nobody asked for.
    """
    outcomes = [
        SweepOutcome(
            policy_id=policy_id, scope="cloudflare",
            store=(POLICY_BY_ID[policy_id].store if policy_id in POLICY_BY_ID else policy_id),
            cutoff=cutoff_for(policy_id, now), dry_run=dry_run,
        )
        for policy_id in ECO_MAINTENANCE_POLICIES
    ]

    endpoint = (os.getenv(ENV_PUBLISHER_ENDPOINT) or "").strip()
    token = (os.getenv(ENV_PUBLISHER_TOKEN) or "").strip()
    if not endpoint or not token:
        for outcome in outcomes:
            outcome.classification = "RETENTION_FAILED"
            outcome.failed = 1
            outcome.defect_code = "ECO_PUBLISHER_UNAVAILABLE"
            outcome.error = (
                f"{ENV_PUBLISHER_ENDPOINT}/{ENV_PUBLISHER_TOKEN} not configured; "
                f"D1 and R2 hard retention cannot run from the platform schedule"
            )
        return outcomes

    if dry_run:
        for outcome in outcomes:
            outcome.classification = "RETENTION_DRY_RUN_SUCCEEDED"
            outcome.note = (
                "publisher-authenticated maintenance would be called; the route "
                "has no plan-only mode, so a dry run contacts nothing"
            )
        return outcomes

    try:
        from jobs.ecodriving_dashboard import secure_delivery_client as sdc  # noqa: PLC0415

        client = sdc.SecureDeliveryClient(
            sdc.HttpSecureDeliveryTransport(endpoint), publisher_token=token,
        )
        payload = client.compact_authorization_state()
    except Exception as exc:
        for outcome in outcomes:
            outcome.classification = "RETENTION_PARTIAL_FAILURE"
            outcome.failed = 1
            outcome.defect_code = "ECO_MAINTENANCE_FAILED"
            outcome.error = f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"[:300]
        return outcomes

    retention = dict(payload.get("hard_retention") or {})
    removed = {
        "cloudflare_d1.eco_session": int(payload.get("expired_sessions_removed") or 0),
        "cloudflare_d1.eco_capability": int(retention.get("capabilities_removed") or 0),
        "cloudflare_d1.eco_publication_operation": int(
            retention.get("publications_removed") or 0),
        "cloudflare_r2.driver_eco_snapshots": int(retention.get("snapshots_removed") or 0),
    }
    examined = {
        "cloudflare_r2.driver_eco_snapshots": int(retention.get("snapshots_examined") or 0),
    }
    snapshot_error = retention.get("snapshot_error")
    for outcome in outcomes:
        outcome.deleted = removed.get(outcome.policy_id, 0)
        outcome.examined = examined.get(outcome.policy_id, outcome.deleted)
        if outcome.policy_id == "cloudflare_r2.driver_eco_snapshots" and snapshot_error:
            outcome.classification = "RETENTION_PARTIAL_FAILURE"
            outcome.failed = 1
            outcome.defect_code = "ECO_R2_SWEEP_FAILED"
            outcome.error = str(snapshot_error)[:200]
        else:
            outcome.classification = "RETENTION_EXECUTION_SUCCEEDED"
    return outcomes


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

LEDGER_TABLE = "ops_control.retention_execution"


def _ledger_has_target_column(cur) -> bool:
    """Is migration 071 applied on this database?

    Asked rather than assumed so a host whose code is newer than its schema
    keeps recording — degraded to the old, collapsing identity — instead of
    silently writing nothing. An observability outage is a worse failure than a
    coarse ledger, and `record_outcomes` is best-effort precisely so that a
    ledger problem never becomes a retention problem.
    """
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
              FROM pg_attribute a
              JOIN pg_class c ON c.oid = a.attrelid
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'ops_control' AND c.relname = 'retention_execution'
               AND a.attname = 'target' AND a.attnum > 0 AND NOT a.attisdropped
        ) AS present
        """
    )
    return bool(_scalar(cur))


def record_outcomes(conn, outcomes: Sequence[SweepOutcome]) -> int:
    """Upsert the last outcome per (policy_id, scope, target). Best effort by design.

    THE TARGET IS PART OF THE IDENTITY, and that is the whole reason this
    function was changed. Several policies sweep more than one relation under
    one scope — three staging tables, fourteen registered client tables, two
    Stage 3 report tables, all per client — and keying on `(policy_id, scope)`
    alone meant every relation's upsert overwrote the previous one. The
    production sweep of 2026-08-29 recorded 152 outcomes into 62 rows, and
    `deleted_count` on a multi-relation policy could not be attributed to a
    relation at all.

    `outcome.store` is that identity: `schema.table` for a relation, the
    absolute path for a filesystem root, the store name for a remote store. It
    is stable across runs and independent of loop order, so re-sweeping a target
    updates its own row and never a sibling's.

    A ledger that could abort the sweep would be a reliability regression: the
    deletions have already committed, and failing here must not make an
    operator believe they did not happen.
    """
    written = 0
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT to_regclass('{LEDGER_TABLE}') IS NOT NULL AS present")
            if not _scalar(cur):
                conn.rollback()
                return 0
            per_target = _ledger_has_target_column(cur)
            for outcome in outcomes:
                if outcome.classification == EXEMPT_CLASSIFICATION:
                    # Nothing executed, so there is no execution to record. The
                    # ledger's `classification` CHECK admits sweep outcomes
                    # only, and `cutoff_ts` is NOT NULL — an exempt store has
                    # neither. It is reported in the run result instead.
                    continue
                target_columns = ", target" if per_target else ""
                target_value = ", %s" if per_target else ""
                conflict = (
                    "(policy_id, scope, target)" if per_target else "(policy_id, scope)"
                )
                values: list[Any] = [outcome.policy_id, outcome.scope]
                if per_target:
                    # Bounded to the column's CHECK. Truncating rather than
                    # failing keeps a long store description from costing the
                    # whole ledger write; every real store name is far shorter.
                    values.append(outcome.store[:200] or "-")
                values += [
                    outcome.cutoff, outcome.dry_run, outcome.classification,
                    outcome.examined, outcome.deleted, outcome.skipped,
                    outcome.failed, outcome.oldest_remaining,
                    json.dumps({
                        "store": outcome.store,
                        "batches": outcome.batches,
                        "unanchored_count": outcome.unanchored,
                        "error": outcome.error,
                    }),
                ]
                cur.execute(
                    f"""
                    INSERT INTO {LEDGER_TABLE}
                        (policy_id, scope{target_columns}, executed_at, cutoff_ts,
                         dry_run, classification, examined_count, deleted_count,
                         skipped_count, failed_count, oldest_remaining_ts,
                         detail, updated_at)
                    VALUES (%s, %s{target_value}, now(), %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, now())
                    ON CONFLICT {conflict} DO UPDATE SET
                        executed_at = EXCLUDED.executed_at,
                        cutoff_ts = EXCLUDED.cutoff_ts,
                        dry_run = EXCLUDED.dry_run,
                        classification = EXCLUDED.classification,
                        examined_count = EXCLUDED.examined_count,
                        deleted_count = EXCLUDED.deleted_count,
                        skipped_count = EXCLUDED.skipped_count,
                        failed_count = EXCLUDED.failed_count,
                        oldest_remaining_ts = EXCLUDED.oldest_remaining_ts,
                        detail = EXCLUDED.detail,
                        updated_at = now()
                    """,
                    tuple(values),
                )
                written += 1
        conn.commit()
    except Exception:
        _safe_rollback(conn)
        return written
    return written


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _platform_dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', '')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _connect(dsn: str):
    import psycopg  # noqa: PLC0415

    return psycopg.connect(dsn, autocommit=False)


def enabled_clients(conn) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row  # noqa: PLC0415

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT client_code, client_db_host, client_db_port, client_db_name,
                   client_db_user, client_db_password_secret_ref, client_db_schema
              FROM workflow_a_control.client_account
             WHERE enabled = true
             ORDER BY client_code
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


def run(
    *,
    execute: bool = False,
    now: datetime | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int | None = None,
    policy_filter: str | None = None,
    client_filter: str | None = None,
    skip_clients: bool = False,
    skip_filesystem: bool = False,
    #: Run the artifact/MinIO ceiling pass through `api/platform_prune.py`.
    #: Off in unit tests, which have neither an object store nor an attested
    #: platform identity.
    skip_artifacts: bool = False,
    #: Drive the Cloudflare Worker's D1/R2 maintenance from this sweep. Off in
    #: unit tests, which have no publisher credential.
    skip_cloudflare: bool = False,
    #: Write the outcome ledger. Turned off for a purely read-only rehearsal
    #: against a live database, where even an observability upsert would be a
    #: production mutation.
    record: bool = True,
    platform_dsn: str | None = None,
    client_dsn_factory: Callable[[Mapping[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Sweep every governed store. Returns a structured, payload-free result."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise HardRetentionError("now must be timezone-aware")
    deadline_cutoff = hard_retention_cutoff(moment)
    dry_run = not execute
    outcomes: list[SweepOutcome] = []

    def wanted(policy_id: str) -> bool:
        return policy_filter is None or policy_id == policy_filter

    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "policy_id": GLOBAL_POLICY_ID,
        "retention_months": HARD_RETENTION_MONTHS,
        # The bare deadline horizon. Every store is swept with its OWN, earlier
        # cutoff (`cutoff_for`), reported per sweep, so an operator can see both
        # the policy and the lead that makes a periodic sweep meet it.
        "deadline_cutoff": deadline_cutoff.isoformat(),
        "backup_topology": PLATFORM_BACKUP_SET.as_dict(),
        "executed_at": moment.isoformat(),
        "dry_run": dry_run,
    }

    conn = None
    lock_held = False
    try:
        conn = _connect(platform_dsn or _platform_dsn())
    except Exception as exc:
        result.update({
            "ok": False, "classification": "RETENTION_FAILED",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "operator_action_required": True, "sweeps": [],
        })
        return result

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_NAMESPACE,))
            lock_held = bool(_scalar(cur))
        conn.rollback()
        if not lock_held:
            result.update({
                "ok": False, "classification": "RETENTION_LOCKED",
                "error": "another hard-retention sweep holds the advisory lock",
                "operator_action_required": False, "sweeps": [],
            })
            return result

        for sweep in PLATFORM_SWEEPS:
            if not wanted(sweep.policy_id):
                continue
            outcomes.append(sweep_table(
                conn, sweep, cutoff=cutoff_for(sweep.policy_id, moment),
                scope="platform", dry_run=dry_run,
                batch_size=batch_size, max_batches=max_batches,
            ))

        if not skip_clients:
            outcomes.extend(_sweep_clients(
                conn, now=moment, dry_run=dry_run, batch_size=batch_size,
                max_batches=max_batches, wanted=wanted, client_filter=client_filter,
                client_dsn_factory=client_dsn_factory,
            ))

        if not skip_artifacts and wanted(ARTIFACT_CEILING_POLICY):
            outcomes.append(sweep_artifact_ceiling(dry_run=dry_run, now=moment))

        if not skip_cloudflare and any(wanted(pid) for pid in ECO_MAINTENANCE_POLICIES):
            outcomes.extend([
                item for item in
                sweep_eco_dashboard_maintenance(dry_run=dry_run, now=moment)
                if wanted(item.policy_id)
            ])

        if not skip_filesystem:
            for fs_sweep in filesystem_sweeps():
                if not wanted(fs_sweep.policy_id):
                    continue
                outcomes.append(sweep_filesystem(
                    fs_sweep, cutoff=cutoff_for(fs_sweep.policy_id, moment),
                    dry_run=dry_run,
                ))

        # Blocked policies are reported explicitly, so "absent from the output"
        # can never be mistaken for "compliant".
        for policy in POLICY_BY_ID.values():
            if policy.status is not Status.BLOCKED_OWNER_DECISION:
                continue
            if not wanted(policy.policy_id):
                continue
            if any(item.policy_id == policy.policy_id for item in outcomes):
                continue
            outcomes.append(SweepOutcome(
                policy_id=policy.policy_id, scope="-", store=policy.store,
                cutoff=cutoff_for(policy.policy_id, moment), dry_run=dry_run,
                classification="RETENTION_SKIPPED_BLOCKED",
                defect_code="BLOCKED_POLICY",
                error="policy is blocked pending an owner decision",
            ))

        ledger_rows = record_outcomes(conn, outcomes) if record else 0
    finally:
        if conn is not None:
            if lock_held:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAMESPACE,))
                    conn.rollback()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

    failures = sum(item.failed for item in outcomes)
    blocked = [item for item in outcomes
               if item.classification == "RETENTION_SKIPPED_BLOCKED"]
    exempt = [item for item in outcomes
              if item.classification == EXEMPT_CLASSIFICATION]
    unanchored = sum(item.unanchored for item in outcomes)
    classification = (
        "RETENTION_PARTIAL_FAILURE" if (failures or unanchored)
        else ("RETENTION_DRY_RUN_SUCCEEDED" if dry_run else "RETENTION_EXECUTION_SUCCEEDED")
    )
    result.update({
        "ok": not failures,
        "classification": classification,
        "operator_action_required": bool(failures or unanchored or blocked),
        "totals": {
            "stores": len(outcomes),
            "examined": sum(item.examined for item in outcomes),
            "deleted": sum(item.deleted for item in outcomes),
            "skipped": sum(item.skipped for item in outcomes),
            "failed": failures,
            "unanchored": unanchored,
            "blocked": len(blocked),
            # Stores with an owner-approved exemption from age-based retention.
            # They are governed and accounted for; they simply have no age
            # candidates, by decision rather than by omission.
            "exempt": len(exempt),
        },
        "owner_exempt_stores": [
            {"policy_id": item.policy_id, "scope": item.scope, "store": item.store}
            for item in exempt
        ],
        "defects": _defect_summary(outcomes),
        "ledger_rows_written": ledger_rows,
        "sweeps": [item.as_dict() for item in outcomes],
    })
    return result


def _defect_summary(outcomes: Sequence[SweepOutcome]) -> dict[str, Any]:
    """Every defect code, with the stores that raised it. The operator to-do list."""
    grouped: dict[str, list[str]] = {}
    for item in outcomes:
        if not item.defect_code or item.defect_code == "RELATION_ABSENT":
            continue
        grouped.setdefault(item.defect_code, []).append(f"{item.scope}:{item.store}")
    return {code: sorted(set(stores)) for code, stores in sorted(grouped.items())}


def _sweep_clients(
    platform_conn, *, now, dry_run, batch_size, max_batches, wanted,
    client_filter, client_dsn_factory,
) -> list[SweepOutcome]:
    outcomes: list[SweepOutcome] = []
    try:
        clients = enabled_clients(platform_conn)
    except Exception as exc:
        outcomes.append(SweepOutcome(
            policy_id="client_db.workflow_a_registered_tables", scope="-",
            store="workflow_a_control.client_account",
            cutoff=hard_retention_cutoff(now), dry_run=dry_run,
            classification="RETENTION_FAILED", failed=1,
            error=f"client inventory unavailable: {type(exc).__name__}",
        ))
        return outcomes

    try:
        sweeps = workflow_a_table_sweeps() + CLIENT_EXTRA_SWEEPS
    except Exception as exc:
        outcomes.append(SweepOutcome(
            policy_id="client_db.workflow_a_registered_tables", scope="-",
            store="jobs.api.telematics.registry",
            cutoff=hard_retention_cutoff(now), dry_run=dry_run,
            classification="RETENTION_FAILED", failed=1,
            error=f"registry unavailable: {type(exc).__name__}",
        ))
        return outcomes

    for account in clients:
        code = str(account.get("client_code") or account.get("client_db_name") or "?")
        if client_filter and code != client_filter:
            continue
        try:
            dsn = (
                client_dsn_factory(account) if client_dsn_factory
                else _client_dsn(account)
            )
            conn = _connect(dsn)
        except Exception as exc:
            outcomes.append(SweepOutcome(
                policy_id="client_db.workflow_a_registered_tables", scope=code,
                store=str(account.get("client_db_name")),
                cutoff=hard_retention_cutoff(now),
                dry_run=dry_run, classification="RETENTION_FAILED", failed=1,
                defect_code="CONNECTION_FAILED",
                error=f"connection failed: {type(exc).__name__}",
            ))
            continue
        try:
            for sweep in sweeps:
                if not wanted(sweep.policy_id):
                    continue
                outcomes.append(sweep_table(
                    conn, sweep, cutoff=cutoff_for(sweep.policy_id, now),
                    scope=code, dry_run=dry_run,
                    batch_size=batch_size, max_batches=max_batches,
                ))
            # Exempt stores are REPORTED, never queried. Saying so per client is
            # what makes "this relation has no age-based retention" visible in
            # exactly the place an operator would otherwise look for its sweep.
            for exempt in CLIENT_EXEMPT_STORES:
                if not wanted(exempt.policy_id):
                    continue
                outcomes.append(exempt_outcome(
                    exempt, scope=code, dry_run=dry_run,
                ))
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return outcomes


def _client_dsn(account: Mapping[str, Any]) -> str:
    from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: PLC0415

    password = resolve_secret(str(account["client_db_password_secret_ref"]))
    return (
        f"host={account['client_db_host']} port={int(account['client_db_port'])} "
        f"dbname={account['client_db_name']} user={account['client_db_user']} "
        f"password={password}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            f"Enforce the global {HARD_RETENTION_MONTHS}-calendar-month retention "
            f"ceiling across every store the shorter lifecycles do not reach"
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete. Without it this is a dry run that mutates nothing.",
    )
    parser.add_argument("--policy", default=None, help="Restrict to one registry policy id")
    parser.add_argument("--client", default=None, help="Restrict to one client code")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--skip-clients", action="store_true")
    parser.add_argument(
        "--skip-artifacts", action="store_true",
        help="Skip the artifact/MinIO ceiling pass (api.platform_prune --hard-ceiling)",
    )
    parser.add_argument(
        "--skip-cloudflare", action="store_true",
        help="Skip the Cloudflare D1/R2 maintenance pass",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help="Do not write ops_control.retention_execution (read-only rehearsal)",
    )
    parser.add_argument("--skip-filesystem", action="store_true")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(REPO_ROOT / ".env", override=False)
    except ImportError:
        pass

    # The same shared, non-blocking backup lock `api/platform_prune.py` takes:
    # deleting rows out from under a running `pg_dump` produces a backup whose
    # internal consistency nobody can reason about afterwards.
    try:
        from api.platform_prune import coordinated_backup_lock  # noqa: PLC0415

        lock = coordinated_backup_lock(REPO_ROOT / "backups" / ".backup.lock")
    except Exception:
        import contextlib  # noqa: PLC0415

        lock = contextlib.nullcontext()

    try:
        with lock:
            result = run(
                execute=bool(args.execute),
                batch_size=int(args.batch_size),
                max_batches=args.max_batches,
                policy_filter=args.policy,
                client_filter=args.client,
                skip_clients=bool(args.skip_clients),
                skip_filesystem=bool(args.skip_filesystem),
                skip_artifacts=bool(args.skip_artifacts),
                skip_cloudflare=bool(args.skip_cloudflare),
                record=not args.no_record,
            )
    except Exception as exc:
        print(json.dumps({
            "schema": RESULT_SCHEMA, "ok": False,
            "classification": "RETENTION_FAILED",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "operator_action_required": True,
        }, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
