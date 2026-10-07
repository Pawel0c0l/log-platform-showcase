from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NamedTuple
from uuid import UUID

import boto3
import psycopg
from psycopg.rows import dict_row


PLAN_SCHEMA = "log-platform-prune-plan/v1"
RESULT_SCHEMA = "log-platform-prune-result/v1"
LOCK_NAMESPACE = "log-platform.platform-prune.v1"
SCOPE = "platform_runs_logs_artifacts"
COMPOSE_INTERNAL_MINIO_ENDPOINT = "minio:9000"
DEFAULT_HOST_MINIO_ENDPOINT = "127.0.0.1:9000"
TERMINAL_RUN_STATUSES = {"SUCCESS", "FAILED", "CANCELED"}

#: Retrospective reconciliation evidence (migration 064). Its FK to
#: `public.runs` is `ON DELETE RESTRICT`, so a run it references can never be
#: pruned; `run_exclusion_reason` retains such runs at plan time so the DELETE
#: is never attempted. Named here rather than inlined because the table is
#: optional — see `build_prune_plan` for the not-yet-migrated case.
RUN_RECONCILIATION_TABLE = "ops_control.run_reconciliation"

#: M4 (`docs/20` §4.5). `workflow_a_control.provider_request_log` has its own
#: retention horizon, deliberately INDEPENDENT of this worker's `--days` value:
#: the log prune runs at 60 days, and the whole point of promoting this evidence
#: out of `public.logs` was that 60 days is less than twice the monthly
#: reconciliation reach. 180 days is long enough to compare two consecutive
#: monthly reconciliations and to characterise seasonal provider behaviour.
#:
#: It rides this worker rather than getting a timer of its own, because a second
#: timer for one table would be an ad-hoc retention system beside the one that
#: already exists. It is a constant rather than a flag so an operator cannot
#: shorten it by passing `--days`.
PROVIDER_REQUEST_LOG_TABLE = "workflow_a_control.provider_request_log"
PROVIDER_REQUEST_LOG_RETENTION_DAYS = 180


#: THE global hard-retention ceiling, imported rather than restated. The ordinary
#: prune horizon above is a SHORTER policy that happens to satisfy the ceiling;
#: the ceiling pass below is what governs the artifacts the ordinary pass
#: deliberately refuses to touch. Neither number is written twice.
from ops.retention_registry import (  # noqa: E402
    GLOBAL_POLICY_ID,
    HARD_RETENTION_MONTHS,
    PrunePolicyConflict,
    get as get_retention_policy,
    hard_retention_cutoff,
    platform_prune_retention_days,
    validate_platform_prune_days,
)

#: The registry entry whose enforcement cutoff the ceiling pass must use. The
#: pass runs weekly and its store is inside the nightly backup set, so deleting
#: at the bare deadline would leave both a sweep-latency gap and a backup copy
#: alive past it. The lead is derived there, never here.
CEILING_POLICY_ID = "platform_db.public.artifacts_reference_excluded"

#: Exclusion reasons the ordinary 60-day pass applies that are RETENTION
#: decisions rather than correctness guards, and therefore stop applying at the
#: ceiling. Everything not listed here still excludes in both modes:
#: `artifact_active_run` (the run is still writing), the two storage-identity
#: refusals (we cannot name the object to delete), `artifact_separate_retention`
#: (the Database Explorer 3-day lifecycle owns those bytes and deletes them
#: itself) and `artifact_generated_report_available` (the product is still
#: offering the file for download; deleting it would silently flip a live report
#: to `Pliki wygasly`).
CEILING_RELAXED_EXCLUSIONS = frozenset({
    "artifact_workflow_b",
    "artifact_retained_reference",
})
class ArtifactReference(NamedTuple):
    """One foreign key to `public.artifacts`, in the terms a DELETE depends on.

    The identity used to be `(table, column)`. That answers "who points at an
    artifact" but not "what happens to them when the artifact goes", and the
    second question is the one this worker's destructive step is decided by. A
    catalog drift that repoints `ON DELETE SET NULL` to `CASCADE` or `RESTRICT`
    leaves `(table, column)` untouched while inverting what the delete does:
    `CASCADE` destroys the referencing row, `RESTRICT` aborts the transaction
    after the objects are already gone from MinIO. Neither may be accepted
    silently, so both are part of the compared identity here.

    Every field is a property the prune's behaviour demonstrably rests on:

    `referencing_table` / `referencing_column`
        who points at the artifact — schema-qualified explicitly rather than
        through `regclass`, whose output depends on `search_path` and would
        render the same relation differently for different roles.
    `referenced_table` / `referenced_column`
        what it points AT. `artifacts` is not required to keep exactly one
        candidate key forever; a reference repointed to some other unique
        column of the same table is a different lifecycle wearing the same
        `(table, column)` name.
    `delete_action`
        what the server does to the referencing row when the prune deletes the
        artifact. This is the guarantee `artifact_exclusion_reason` is written
        against.
    `referencing_column_nullable`
        whether `SET NULL` is even legal. A `SET NULL` reference over a
        `NOT NULL` column is a delete that raises at runtime instead of
        preserving history — the failure mode is a mid-prune abort, so it is
        pinned rather than inferred from `delete_action` alone.

    `delete_action_enforced`
        whether the referential-action triggers that IMPLEMENT `delete_action`
        are actually enabled. `pg_constraint.confdeltype` is a declaration;
        `ON DELETE SET NULL` is carried out by a trigger on the referenced
        table, and a superuser can disable it while leaving every other field
        above untouched. The declared action would then be a fiction: the
        DELETE would leave a dangling reference instead of nulling it.

    `ON UPDATE` is deliberately absent: this worker never updates an artifact's
    primary key, so no destructive behaviour depends on it.
    """

    referencing_table: str
    referencing_column: str
    referenced_table: str
    referenced_column: str
    delete_action: str
    referencing_column_nullable: bool
    delete_action_enforced: bool = True


#: The EXACT set of foreign keys allowed to reference `public.artifacts`, each
#: pinned to the catalog metadata its retention decision relies on. It is
#: compared for equality, not containment: an unreviewed reference — in either
#: direction — stops the prune with `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` before
#: anything is planned. That is the point. A new FK is a new way for a delete to
#: destroy or corrupt something, so each one is admitted only after its
#: retention semantics have been established and taught to
#: `artifact_exclusion_reason`.
#:
#: The five pre-Portal entries are transcribed from the migrations that created
#: them — 024, 026, 043, 048 — and change no behaviour: they carried these
#: actions before this contract could see them. Only the Portal entry is new.
EXPECTED_ARTIFACT_REFERENCES = frozenset(
    {
        # Curation, migrations 024 and 026. ON DELETE CASCADE over a NOT NULL
        # column: deleting the artifact DESTROYS the curation without a word,
        # which is why `artifact_retained_reference` retains at plan time.
        ArtifactReference(
            "public.artifact_metadata_overrides", "artifact_id",
            "public.artifacts", "artifact_id", "CASCADE", False,
        ),
        ArtifactReference(
            "public.artifact_tags", "artifact_id",
            "public.artifacts", "artifact_id", "CASCADE", False,
        ),
        ArtifactReference(
            "public.artifact_virtual_folder_items", "artifact_id",
            "public.artifacts", "artifact_id", "CASCADE", False,
        ),
        # Database Explorer exports, migration 043. SET NULL over a nullable
        # column; the job row has its own retention horizon.
        ArtifactReference(
            "public.database_export_jobs", "artifact_id",
            "public.artifacts", "artifact_id", "SET NULL", True,
        ),
        # Workflow B Stage 2, migration 048. SET NULL over a nullable column.
        ArtifactReference(
            "ingest.raw_file", "stage2_cleaned_artifact_id",
            "public.artifacts", "artifact_id", "SET NULL", True,
        ),
        # Portal V1, migration 068. A generated-report member holds the
        # report's domain identity while `artifacts` holds its bytes — the same
        # split `database_export_jobs` uses. The reference is ON DELETE SET
        # NULL over a NULLABLE column precisely so object cleanup can never
        # destroy the history row ("the member outlives its bytes", 068).
        #
        # THOSE TWO PROPERTIES ARE THE GUARANTEE, so both are pinned. Under
        # CASCADE the same prune would erase the report's history instead of
        # preserving it; under RESTRICT it would abort the transaction after
        # MinIO objects were already deleted; over a NOT NULL column the
        # SET NULL itself would raise mid-delete. All three leave
        # `(table, column)` identical, and all three are refused here.
        #
        # SET NULL is NOT a licence to delete freely, and — verified against a
        # real catalog, not inferred from the DDL — the database will NOT stop
        # us either. 068 pairs the availability CHECK
        #
        #     CHECK ((NOT is_available) OR (artifact_id IS NOT NULL))
        #
        # with a BEFORE trigger, `portal_generated_report_files_availability`,
        # that flips `is_available` to FALSE whenever the reference becomes
        # NULL. Its own comment is explicit: "object cleanup must never be
        # BLOCKED by, and must never contradict, the availability marker". So
        # deleting the artifact of a LIVE, downloadable report does not raise —
        # it silently converts that report to `Pliki wygasły`.
        #
        # That is precisely why the exclusion in `artifact_exclusion_reason` is
        # not optional. Nothing in the schema defends a member the product is
        # still offering; the only thing standing between a live generated
        # report and the 60-day horizon is this worker refusing to plan it.
        # Unlike the reconciliation FK, which fails loud on RESTRICT, this one
        # fails SILENT.
        ArtifactReference(
            "public.portal_generated_report_files", "artifact_id",
            "public.artifacts", "artifact_id", "SET NULL", True,
        ),
    }
)


class PlatformPruneError(RuntimeError):
    def __init__(self, code: str, *, operator_action_required: bool = True):
        self.code = code
        self.operator_action_required = operator_action_required
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    environment: str
    platform_uuid: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str


@dataclass(frozen=True, slots=True)
class ArtifactCandidate:
    artifact_id: str
    storage_key: str


@dataclass(slots=True)
class PrunePlan:
    cutoff: datetime
    #: `None` in ceiling mode: the horizon there is 13 CALENDAR months, which is
    #: not a day count and must not be reported as one.
    retention_days: int | None
    #: True when this plan was built against the global hard-retention ceiling.
    hard_ceiling: bool = False
    artifacts: list[ArtifactCandidate] = field(default_factory=list)
    log_ids: list[int] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    #: M4 evidence rows past their own 180-day horizon. Carried as a count and a
    #: cutoff rather than an id list: the delete is a single bounded statement
    #: against an indexed timestamp, and materializing hundreds of thousands of
    #: UUIDs to re-send them would be strictly worse.
    provider_request_log_cutoff: datetime | None = None
    provider_request_log_rows: int = 0

    def add_exclusion(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1

    def safe_summary(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "scope": SCOPE,
            "retention_days": self.retention_days,
            "hard_ceiling": self.hard_ceiling,
            "cutoff_is_deadline_minus_lead": self.hard_ceiling,
            "policy_id": (
                GLOBAL_POLICY_ID if self.hard_ceiling
                else "platform_db.public.runs+logs+artifacts"
            ),
            "retention_months": HARD_RETENTION_MONTHS if self.hard_ceiling else None,
            "cutoff": self.cutoff.isoformat(),
            "cutoff_timezone": "UTC",
            "candidates": {
                "artifact_rows": len(self.artifacts),
                "minio_objects": len(self.artifacts),
                "log_rows": len(self.log_ids),
                "run_rows": len(self.run_ids),
                "provider_request_log_rows": self.provider_request_log_rows,
            },
            "provider_request_log_retention_days": (
                PROVIDER_REQUEST_LOG_RETENTION_DAYS
            ),
            "provider_request_log_cutoff": (
                None if self.provider_request_log_cutoff is None
                else self.provider_request_log_cutoff.isoformat()
            ),
            "excluded": dict(sorted(self.excluded.items())),
        }


def validate_retention_days(value: object) -> int:
    """Parse `--days`, then prove the CENTRAL POLICY admits it. Fails closed.

    The literal `--days 60` in `ops/systemd/log-platform-prune.service` and the
    `Retention.days(60)` on three registry entries used to be independent
    numbers that happened to agree — nothing compared them, so a unit edit could
    have kept run/log/artifact history for months past what the registry told an
    operator it kept, silently. The upper bound is now the registry's declared
    horizon rather than an arbitrary 3650, so a longer `--days` is refused here
    at startup, before anything is planned.

    Shorter stays allowed: it deletes earlier, which can only reduce how long a
    record lives, and an operator reclaiming disk in an incident should not have
    to edit the registry to do it.
    """
    if isinstance(value, bool):
        raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID") from exc
    if str(value).strip() != str(days):
        raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID")
    try:
        return validate_platform_prune_days(days, source="--days")
    except PrunePolicyConflict as exc:
        raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID") from exc


def calculate_cutoff(*, now_utc: datetime, retention_days: int) -> datetime:
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise PlatformPruneError("PRUNE_TIMEZONE_INVALID")
    return now_utc.astimezone(timezone.utc) - timedelta(days=retention_days)


def _required(values: Mapping[str, str], name: str) -> str:
    value = str(values.get(name) or "").strip()
    if not value:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_MISSING")
    return value


def load_runtime_identity(values: Mapping[str, str] | None = None) -> RuntimeIdentity:
    source = values if values is not None else os.environ
    environment = _required(source, "LOG_PLATFORM_TARGET_ENVIRONMENT")
    if environment not in {"local_dev", "staging", "production"}:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_INVALID")
    platform_uuid = _required(source, "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID")
    try:
        if str(UUID(platform_uuid)) != platform_uuid:
            raise ValueError
    except ValueError as exc:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_INVALID") from exc
    expected_host = _required(source, "LOG_PLATFORM_EXPECTED_POSTGRES_HOST")
    expected_port_text = _required(source, "LOG_PLATFORM_EXPECTED_POSTGRES_PORT")
    expected_db = _required(source, "LOG_PLATFORM_EXPECTED_POSTGRES_DB")
    expected_user = _required(source, "LOG_PLATFORM_EXPECTED_POSTGRES_USER")
    try:
        expected_port = int(expected_port_text)
    except ValueError as exc:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_INVALID") from exc
    effective = (
        str(source.get("POSTGRES_HOST") or "").strip(),
        str(source.get("POSTGRES_PORT") or "").strip(),
        str(source.get("POSTGRES_DB") or "").strip(),
        str(source.get("POSTGRES_USER") or "").strip(),
    )
    expected = (expected_host, str(expected_port), expected_db, expected_user)
    if effective != expected:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_MISMATCH")
    return RuntimeIdentity(environment, platform_uuid, expected_host, expected_port, expected_db, expected_user)


def attest_platform_identity(conn, identity: RuntimeIdentity) -> None:
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '10s'")
        cur.execute("SELECT current_database() AS database_name, current_user AS database_user")
        connection = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT environment, database_identity_id::text AS database_identity_id,
                   database_role, database_name
            FROM ops_control.environment_identity
            WHERE identity_key = 'primary'
            """
        )
        markers = [dict(row) for row in cur.fetchall()]
    if len(markers) != 1:
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_MISMATCH")
    marker = markers[0]
    if (
        connection.get("database_name") != identity.postgres_db
        or connection.get("database_user") != identity.postgres_user
        or marker.get("environment") != identity.environment
        or marker.get("database_identity_id") != identity.platform_uuid
        or marker.get("database_role") != "platform"
        or marker.get("database_name") != identity.postgres_db
    ):
        raise PlatformPruneError("PRUNE_ENVIRONMENT_IDENTITY_MISMATCH")


def lock_reviewed_relations(cur) -> None:
    """Hold the reviewed relations still for the rest of the transaction.

    Validating the catalog and then reading it are two statements, and between
    them a migration could commit. Without this the worker could validate
    `ON DELETE SET NULL`, then plan and delete under a freshly committed
    `CASCADE` — a fail-open that no amount of care inside the validator can
    close, because the drift happens after it returns.

    `ACCESS SHARE` is the same mode the planner's own SELECTs take a moment
    later, so this changes no lock ordering; it only takes them EARLIER, before
    the contract is trusted. It conflicts with `ACCESS EXCLUSIVE`, which is what
    `ALTER TABLE ... DROP CONSTRAINT` needs, so a reference cannot be repointed
    underneath a validated plan. Locks are released with the transaction.

    A relation that does not exist is skipped rather than raising: an
    unmigrated database must fail as `PRUNE_REFERENCE_CONTRACT_AMBIGUOUS` from
    the validator below, not as a dependency error from here.
    """
    relations = sorted(
        {reference.referencing_table for reference in EXPECTED_ARTIFACT_REFERENCES}
        | {reference.referenced_table for reference in EXPECTED_ARTIFACT_REFERENCES}
    )
    for relation in relations:
        cur.execute("SELECT to_regclass(%s) AS relation_oid", (relation,))
        row = cur.fetchone()
        if not row or row.get("relation_oid") is None:
            continue
        # `relation` comes from this module's own constant, never from input.
        cur.execute(f"LOCK TABLE {relation} IN ACCESS SHARE MODE")


def validate_artifact_reference_contract(cur) -> None:
    """Refuse to plan anything unless the catalog matches the reviewed contract.

    Read the whole FK, not just its endpoints. `confdeltype` is what decides
    what the prune's DELETE does to every referencing row, and `attnotnull` is
    what decides whether a `SET NULL` action can execute at all, so both are
    compared. `pg_namespace` is joined explicitly instead of casting through
    `regclass`, whose text output silently omits any schema on `search_path`
    and would therefore depend on the connecting role rather than the schema.

    `confdeltype` is only a declaration, so the triggers that carry the action
    out are checked too: a disabled referential-action trigger leaves every
    other field of the constraint intact while the action never fires.
    """
    # `session_replication_role = replica` suppresses ORDINARY triggers, which
    # is what every foreign key's referential action is. In that mode each
    # constraint below still reads as perfectly enforced — `tgenabled` is 'O',
    # the catalog is untouched — while `ON DELETE SET NULL` silently does
    # nothing and the DELETE leaves a dangling reference behind.
    #
    # Verified rather than SET: the setting is superuser-only, so a worker that
    # forced it would simply fail on a least-privilege role. Refusing is both
    # portable and the fail-closed answer — a session configured to skip
    # referential integrity is not one this contract can vouch for.
    cur.execute("SELECT current_setting('session_replication_role') AS replication_role")
    role_row = cur.fetchone()
    if not role_row or str(role_row.get("replication_role") or "") != "origin":
        raise PlatformPruneError("PRUNE_REFERENCE_CONTRACT_AMBIGUOUS")
    cur.execute(
        """
        SELECT referencing_ns.nspname || '.' || referencing_rel.relname AS referencing_table,
               referencing_attr.attname AS referencing_column,
               referenced_ns.nspname || '.' || referenced_rel.relname AS referenced_table,
               referenced_attr.attname AS referenced_column,
               CASE constraint_row.confdeltype
                    WHEN 'a' THEN 'NO ACTION'
                    WHEN 'r' THEN 'RESTRICT'
                    WHEN 'c' THEN 'CASCADE'
                    WHEN 'n' THEN 'SET NULL'
                    WHEN 'd' THEN 'SET DEFAULT'
                    ELSE 'UNRECOGNISED'
               END AS delete_action,
               NOT referencing_attr.attnotnull AS referencing_column_nullable,
               -- The declared action is only real if the triggers that carry
               -- it out will fire. `tgenabled` is 'O'/'A' when enabled on
               -- origin; 'D' is disabled and 'R' fires only in replica mode,
               -- which for this worker is the same as disabled.
               (EXISTS (SELECT 1 FROM pg_trigger enforcing
                         WHERE enforcing.tgconstraint = constraint_row.oid
                           AND enforcing.tgrelid = constraint_row.confrelid)
                AND NOT EXISTS (SELECT 1 FROM pg_trigger enforcing
                                 WHERE enforcing.tgconstraint = constraint_row.oid
                                   AND enforcing.tgenabled NOT IN ('O', 'A'))
               ) AS delete_action_enforced,
               cardinality(constraint_row.conkey) AS referencing_column_count
        FROM pg_constraint constraint_row
        JOIN pg_class referencing_rel ON referencing_rel.oid = constraint_row.conrelid
        JOIN pg_namespace referencing_ns ON referencing_ns.oid = referencing_rel.relnamespace
        JOIN pg_class referenced_rel ON referenced_rel.oid = constraint_row.confrelid
        JOIN pg_namespace referenced_ns ON referenced_ns.oid = referenced_rel.relnamespace
        JOIN unnest(constraint_row.conkey, constraint_row.confkey)
             WITH ORDINALITY AS key_column(attnum, confattnum, ord) ON true
        JOIN pg_attribute referencing_attr
          ON referencing_attr.attrelid = constraint_row.conrelid
         AND referencing_attr.attnum = key_column.attnum
        JOIN pg_attribute referenced_attr
          ON referenced_attr.attrelid = constraint_row.confrelid
         AND referenced_attr.attnum = key_column.confattnum
        WHERE constraint_row.contype = 'f'
          AND constraint_row.confrelid = 'public.artifacts'::regclass
        ORDER BY 1, 2
        """
    )
    observed = set()
    for row in cur.fetchall():
        # A COMPOSITE key to `artifacts` is none of the six single-column
        # references below, but it would arrive here as several rows that each
        # look like one. Refuse it explicitly rather than let the per-column
        # rows be compared as if they were whole references.
        if int(row["referencing_column_count"]) != 1:
            raise PlatformPruneError("PRUNE_REFERENCE_CONTRACT_AMBIGUOUS")
        observed.add(
            ArtifactReference(
                referencing_table=str(row["referencing_table"]),
                referencing_column=str(row["referencing_column"]),
                referenced_table=str(row["referenced_table"]),
                referenced_column=str(row["referenced_column"]),
                delete_action=str(row["delete_action"]),
                referencing_column_nullable=bool(row["referencing_column_nullable"]),
                delete_action_enforced=bool(row["delete_action_enforced"]),
            )
        )
    if observed != EXPECTED_ARTIFACT_REFERENCES:
        raise PlatformPruneError("PRUNE_REFERENCE_CONTRACT_AMBIGUOUS")


def artifact_exclusion_reason(
    row: Mapping[str, Any], *, hard_ceiling: bool = False,
) -> str | None:
    reason = _artifact_exclusion_reason(row)
    if hard_ceiling and reason in CEILING_RELAXED_EXCLUSIONS:
        return None
    return reason


def _artifact_exclusion_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("run_status") == "RUNNING":
        return "artifact_active_run"
    if row.get("raw_file_id") is not None or row.get("workflow_name") == "workflow_b":
        return "artifact_workflow_b"
    if row.get("workflow_name") == "database_explorer" or row.get("export_reference"):
        return "artifact_separate_retention"
    # Portal V1 generated reports (migration 068). Distinct from
    # `artifact_retained_reference` below: that class protects user curation
    # that would CASCADE away, this one protects a member the product is still
    # offering for download — which the database would let us delete silently,
    # flipping the report to `Pliki wygasły` on its own trigger.
    # An unavailable member deliberately does NOT reach here: once availability
    # has been dropped, the bytes are no longer offered and the artifact returns
    # to the ordinary retention horizon.
    if row.get("generated_report_reference"):
        return "artifact_generated_report_available"
    if row.get("metadata_reference") or row.get("tag_reference") or row.get("folder_reference"):
        return "artifact_retained_reference"
    if str(row.get("storage_backend") or "") != "S3":
        return "artifact_storage_backend_unsupported"
    storage_key = row.get("storage_key")
    if not isinstance(storage_key, str) or not storage_key or storage_key != storage_key.strip():
        return "artifact_storage_identity_ambiguous"
    return None


def log_exclusion_reason(row: Mapping[str, Any]) -> str | None:
    status = row.get("run_status")
    if status == "RUNNING":
        return "log_active_run"
    if status is not None and status not in TERMINAL_RUN_STATUSES:
        return "log_nonterminal_run"
    return None


def run_exclusion_reason(row: Mapping[str, Any]) -> str | None:
    status = row.get("status")
    if status == "RUNNING":
        return "run_active"
    if status not in TERMINAL_RUN_STATUSES:
        return "run_nonterminal"
    # A retrospectively reconciled run is retained for exactly the same reason
    # logs and artifacts retain one: something durable still references it.
    #
    # This is not merely tidiness. `ops_control.run_reconciliation` (migration
    # 064) holds the FK `ON DELETE RESTRICT` that makes reconciliation evidence
    # survive deletion of its subject. Pruning such a run would therefore fail
    # the `DELETE FROM runs` — and because `_delete_objects` runs BEFORE the
    # database transaction, that failure would roll back every DB deletion in
    # the batch while the MinIO objects were already gone. Excluding the run at
    # plan time is what keeps object deletion and row deletion agreeing.
    if row.get("has_reconciliation"):
        return "run_retained_reference"
    if row.get("has_logs") or row.get("has_artifacts"):
        return "run_retained_reference"
    return None


def build_prune_plan(
    cur, *, cutoff: datetime, retention_days: int | None, lock_rows: bool,
    now_utc: datetime | None = None, hard_ceiling: bool = False,
) -> PrunePlan:
    # Pin the schema the unqualified relation names below resolve to, BEFORE
    # anything is validated or read. The reference contract is checked against
    # `public.artifacts` explicitly, so without this the guard and the delete
    # could be talking about different tables: a `search_path` set on the role
    # or the database — `SET ROLE`, `ALTER DATABASE ... SET search_path`, an
    # inherited `PGOPTIONS` — that puts another schema first would leave the
    # contract validating `public` while the planner read, and `_delete_*`
    # deleted, a shadow relation. Every out-of-`public` relation this worker
    # touches (`ingest.raw_file`, `ops_control.run_reconciliation`,
    # `workflow_a_control.provider_request_log`) is named schema-qualified, so
    # `public` is the only entry the planner needs.
    #
    # `SET LOCAL` scopes this to the caller's transaction and is reverted with
    # it, so the worker never mutates a session it did not open.
    cur.execute("SET LOCAL search_path = pg_catalog, public")
    lock_reviewed_relations(cur)
    validate_artifact_reference_contract(cur)
    plan = PrunePlan(cutoff=cutoff, retention_days=retention_days,
                     hard_ceiling=hard_ceiling)
    artifact_lock = " FOR UPDATE OF artifact" if lock_rows else ""
    cur.execute(
        """
        SELECT artifact.artifact_id::text AS artifact_id, artifact.storage_key,
               artifact.storage_backend, artifact.raw_file_id, artifact.workflow_name,
               run.status AS run_status,
               EXISTS (SELECT 1 FROM artifact_metadata_overrides ref WHERE ref.artifact_id = artifact.artifact_id) AS metadata_reference,
               EXISTS (SELECT 1 FROM artifact_tags ref WHERE ref.artifact_id = artifact.artifact_id) AS tag_reference,
               EXISTS (SELECT 1 FROM artifact_virtual_folder_items ref WHERE ref.artifact_id = artifact.artifact_id) AS folder_reference,
               EXISTS (SELECT 1 FROM database_export_jobs ref WHERE ref.artifact_id = artifact.artifact_id) AS export_reference,
               -- Availability-scoped ON PURPOSE. An expired member has already
               -- been swept to `is_available = FALSE` by
               -- `ReportPublication.expire_due_members`, its bytes are no longer
               -- offered, and `SET NULL` is then the designed cleanup that keeps
               -- the history row. Only an AVAILABLE member protects its object.
               -- The relation is guaranteed to exist here: the reference
               -- contract above is exact, so this statement is unreachable
               -- unless migration 068 is applied.
               EXISTS (SELECT 1 FROM portal_generated_report_files ref
                        WHERE ref.artifact_id = artifact.artifact_id
                          AND ref.is_available) AS generated_report_reference
        FROM artifacts artifact
        LEFT JOIN runs run ON run.run_id = artifact.run_id
        WHERE artifact.created_at < %s
        ORDER BY artifact.artifact_id
        """ + artifact_lock,
        (cutoff,),
    )
    for row in cur.fetchall():
        reason = artifact_exclusion_reason(row, hard_ceiling=hard_ceiling)
        if reason:
            plan.add_exclusion(reason)
        else:
            plan.artifacts.append(ArtifactCandidate(str(row["artifact_id"]), str(row["storage_key"])))

    log_lock = " FOR UPDATE OF log_row" if lock_rows else ""
    cur.execute(
        """
        SELECT log_row.id, run.status AS run_status
        FROM logs log_row
        LEFT JOIN runs run ON run.run_id = log_row.run_id
        WHERE log_row.ts < %s
        ORDER BY log_row.id
        """ + log_lock,
        (cutoff,),
    )
    for row in cur.fetchall():
        reason = log_exclusion_reason(row)
        if reason:
            plan.add_exclusion(reason)
        else:
            plan.log_ids.append(int(row["id"]))

    run_lock = " FOR UPDATE OF run" if lock_rows else ""
    # Migration 064 is additive and may not be applied yet. An older schema — or
    # a release rolled back below 064 — must keep pruning normally rather than
    # failing closed on a relation it has no reason to have, exactly as the M4
    # provider-request-log horizon below already does. When the table is absent
    # there can be no reconciled run, so a constant false is the correct answer
    # rather than a degraded one.
    cur.execute("SELECT to_regclass(%s) AS relation", (RUN_RECONCILIATION_TABLE,))
    reconciliation_row = cur.fetchone()
    reconciliation_present = (
        reconciliation_row is not None and reconciliation_row.get("relation") is not None
    )
    if reconciliation_present:
        reconciliation_select = (
            f"EXISTS (SELECT 1 FROM {RUN_RECONCILIATION_TABLE} ref "
            "WHERE ref.run_id = run.run_id) AS has_reconciliation"
        )
    else:
        plan.add_exclusion("run_reconciliation_absent")
        reconciliation_select = "false AS has_reconciliation"
    cur.execute(
        f"""
        SELECT run.run_id::text AS run_id, run.status,
               EXISTS (SELECT 1 FROM logs ref WHERE ref.run_id = run.run_id) AS has_logs,
               EXISTS (SELECT 1 FROM artifacts ref WHERE ref.run_id = run.run_id) AS has_artifacts,
               {reconciliation_select}
        FROM runs run
        WHERE run.started_at < %s
        ORDER BY run.run_id
        """ + run_lock,
        (cutoff,),
    )
    for row in cur.fetchall():
        reason = run_exclusion_reason(row)
        if reason:
            plan.add_exclusion(reason)
        else:
            plan.run_ids.append(str(row["run_id"]))

    # M4 evidence, on its own horizon. Counted here so a dry run reports it
    # exactly as it reports everything else, and skipped entirely when the table
    # does not exist — an environment that has not applied migration 061 must
    # keep pruning normally rather than failing closed on a relation it has no
    # reason to have yet.
    plan.provider_request_log_cutoff = calculate_cutoff(
        # Derived from the same `now` the primary cutoff was, so the two
        # horizons are two offsets from one instant rather than two clock reads.
        now_utc=(
            now_utc if now_utc is not None
            # Only reachable in day-horizon mode: the ceiling path always passes
            # `now_utc` explicitly, because a calendar cutoff cannot be inverted
            # into an instant by adding days back to it.
            else cutoff + timedelta(days=int(retention_days or 0))
        ),
        retention_days=PROVIDER_REQUEST_LOG_RETENTION_DAYS,
    )
    cur.execute("SELECT to_regclass(%s) AS relation", (PROVIDER_REQUEST_LOG_TABLE,))
    relation_row = cur.fetchone()
    if relation_row is not None and relation_row.get("relation") is not None:
        cur.execute(
            f"SELECT count(*) AS rows FROM {PROVIDER_REQUEST_LOG_TABLE} "
            "WHERE recorded_at < %s",
            (plan.provider_request_log_cutoff,),
        )
        plan.provider_request_log_rows = int(cur.fetchone()["rows"])
    else:
        plan.add_exclusion("provider_request_log_absent")
    return plan


def _delete_objects(s3_client, bucket: str, candidates: list[ArtifactCandidate]) -> int:
    deleted = 0
    for candidate in candidates:
        try:
            s3_client.delete_object(Bucket=bucket, Key=candidate.storage_key)
        except Exception as exc:
            raise PlatformPruneError("PRUNE_OBJECT_STORE_DELETE_FAILED") from exc
        deleted += 1
    return deleted


def _delete_database_rows(cur, plan: PrunePlan) -> dict[str, int]:
    result = {
        "artifact_rows": 0, "log_rows": 0, "run_rows": 0,
        "provider_request_log_rows": 0,
    }
    if plan.artifacts:
        cur.execute("DELETE FROM artifacts WHERE artifact_id = ANY(%s::uuid[])", ([item.artifact_id for item in plan.artifacts],))
        result["artifact_rows"] = cur.rowcount
        if cur.rowcount != len(plan.artifacts):
            raise PlatformPruneError("PRUNE_DATABASE_STATE_DRIFT")
    if plan.log_ids:
        cur.execute("DELETE FROM logs WHERE id = ANY(%s::bigint[])", (plan.log_ids,))
        result["log_rows"] = cur.rowcount
        if cur.rowcount != len(plan.log_ids):
            raise PlatformPruneError("PRUNE_DATABASE_STATE_DRIFT")
    if plan.run_ids:
        cur.execute("DELETE FROM runs WHERE run_id = ANY(%s::uuid[])", (plan.run_ids,))
        result["run_rows"] = cur.rowcount
        if cur.rowcount != len(plan.run_ids):
            raise PlatformPruneError("PRUNE_DATABASE_STATE_DRIFT")
    if plan.provider_request_log_rows and plan.provider_request_log_cutoff:
        # Bounded by the same predicate the plan counted under, inside the same
        # SERIALIZABLE transaction, so "counted" and "deleted" cannot disagree
        # about which rows they meant. The row is insert-only and immutable, so
        # nothing can move across the cutoff underneath this.
        cur.execute(
            f"DELETE FROM {PROVIDER_REQUEST_LOG_TABLE} WHERE recorded_at < %s",
            (plan.provider_request_log_cutoff,),
        )
        result["provider_request_log_rows"] = cur.rowcount
        if cur.rowcount != plan.provider_request_log_rows:
            raise PlatformPruneError("PRUNE_DATABASE_STATE_DRIFT")
    return result


def run_prune(*, connection_factory: Callable[[], Any], s3_client: Any, bucket: str,
              identity: RuntimeIdentity, retention_days: int | None, dry_run: bool,
              now_utc: datetime | None = None,
              hard_ceiling: bool = False,
              ceiling_cutoff: datetime | None = None) -> dict[str, Any]:
    """Plan and optionally apply one prune pass.

    Two horizons, one planner. `hard_ceiling=False` is the ordinary day-count
    pass an operator has always run. `hard_ceiling=True` is the global
    13-calendar-month pass: the same identity attestation, the same reference
    contract, the same advisory lock, the same SERIALIZABLE transaction and the
    same object-before-row ordering — only the cutoff and two retention-class
    exclusions differ. Reusing the planner is the point: a second artifact
    deleter with its own idea of what is safe is exactly what this codebase
    must not grow.
    """
    prune_now = now_utc or datetime.now(timezone.utc)
    if hard_ceiling:
        if retention_days is not None:
            raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID")
        days: int | None = None
        # The DEADLINE is `now - 13 calendar months`; what this pass must
        # actually use is earlier, by the lead the registry derives from this
        # policy's maintenance cycle and the audited backup topology. A caller
        # may inject it (the sweep already resolved it); otherwise it is read
        # from the registry, so the number exists in exactly one place.
        cutoff = ceiling_cutoff or get_retention_policy(
            CEILING_POLICY_ID
        ).enforcement_cutoff(prune_now)
        if cutoff is None:  # pragma: no cover - the policy is age-based
            cutoff = hard_retention_cutoff(prune_now)
        if cutoff > prune_now:
            raise PlatformPruneError("PRUNE_RETENTION_CONFIGURATION_INVALID")
    else:
        days = validate_retention_days(retention_days)
        cutoff = calculate_cutoff(now_utc=prune_now, retention_days=days)
    try:
        conn = connection_factory()
    except Exception as exc:
        raise PlatformPruneError("PRUNE_DATABASE_UNAVAILABLE") from exc
    lock_acquired = False
    try:
        attest_platform_identity(conn, identity)
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (LOCK_NAMESPACE,))
            lock_acquired = bool(cur.fetchone()["locked"])
        conn.rollback()
        if not lock_acquired:
            raise PlatformPruneError("PRUNE_LOCKED", operator_action_required=False)
        s3_client.head_bucket(Bucket=bucket)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY" if dry_run else "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            cur.execute("SET LOCAL lock_timeout = '5s'")
            cur.execute("SET LOCAL statement_timeout = '15min'")
            plan = build_prune_plan(
                cur, cutoff=cutoff, retention_days=days,
                lock_rows=not dry_run, now_utc=prune_now,
                hard_ceiling=hard_ceiling,
            )
            if dry_run:
                conn.rollback()
                mutations = {"minio_objects_deleted": 0, "artifact_rows_deleted": 0, "log_rows_deleted": 0, "run_rows_deleted": 0, "provider_request_log_rows_deleted": 0}
                classification = "PRUNE_DRY_RUN_SUCCEEDED"
            else:
                objects_deleted = _delete_objects(s3_client, bucket, plan.artifacts)
                db_deleted = _delete_database_rows(cur, plan)
                conn.commit()
                mutations = {"minio_objects_deleted": objects_deleted, "artifact_rows_deleted": db_deleted["artifact_rows"], "log_rows_deleted": db_deleted["log_rows"], "run_rows_deleted": db_deleted["run_rows"], "provider_request_log_rows_deleted": db_deleted["provider_request_log_rows"]}
                classification = "PRUNE_EXECUTION_SUCCEEDED"
        return {"schema": RESULT_SCHEMA, "ok": True, "classification": classification,
                "dry_run": dry_run, "operator_action_required": False,
                "plan": plan.safe_summary(), "mutations": mutations}
    except PlatformPruneError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise PlatformPruneError("PRUNE_DEPENDENCY_FAILED") from exc
    finally:
        if lock_acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAMESPACE,))
                conn.rollback()
            except Exception:
                pass
        conn.close()


@contextmanager
def coordinated_backup_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_metadata = path.parent.lstat()
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
    ):
        raise PlatformPruneError("PRUNE_BACKUP_LOCK_UNSAFE")
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise PlatformPruneError("PRUNE_BACKUP_LOCK_UNSAFE") from exc
    handle = os.fdopen(descriptor, "a+")
    try:
        os.fchmod(handle.fileno(), 0o600)
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise PlatformPruneError("PRUNE_BACKUP_LOCK_UNSAFE")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PlatformPruneError("PRUNE_BACKUP_ACTIVE", operator_action_required=False) from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _connection_factory(identity: RuntimeIdentity) -> Callable[[], Any]:
    password = os.getenv("POSTGRES_PASSWORD", "")
    def connect():
        return psycopg.connect(host=identity.postgres_host, port=identity.postgres_port,
                               dbname=identity.postgres_db, user=identity.postgres_user,
                               password=password, row_factory=dict_row)
    return connect


def select_host_minio_endpoint(values: Mapping[str, str]) -> str:
    if "MINIO_HOST_ENDPOINT" in values:
        endpoint = str(values.get("MINIO_HOST_ENDPOINT") or "").strip()
        if not endpoint:
            raise PlatformPruneError("PRUNE_HOST_MINIO_ENDPOINT_INVALID")
        return endpoint
    inherited = str(values.get("MINIO_ENDPOINT") or "").strip()
    if not inherited:
        raise PlatformPruneError("PRUNE_HOST_MINIO_ENDPOINT_MISSING")
    if inherited == COMPOSE_INTERNAL_MINIO_ENDPOINT:
        return DEFAULT_HOST_MINIO_ENDPOINT
    return inherited


def _s3_client(values: Mapping[str, str] | None = None):
    source = values if values is not None else os.environ
    endpoint = select_host_minio_endpoint(source)
    secure = str(source.get("MINIO_SECURE", "0")) in {"1", "true", "True"}
    return boto3.client("s3", endpoint_url=("https://" if secure else "http://") + endpoint,
                        aws_access_key_id=_required(source, "MINIO_ROOT_USER"),
                        aws_secret_access_key=_required(source, "MINIO_ROOT_PASSWORD"))


def _load_repo_env(repo_root: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(repo_root / ".env", override=False)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed platform retention prune")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    horizon = parser.add_mutually_exclusive_group(required=True)
    horizon.add_argument(
        "--days",
        help=(
            f"Ordinary day-count horizon. The central policy in "
            f"ops/retention_registry.py declares "
            f"{platform_prune_retention_days()}; a longer value is refused, a "
            f"shorter one is accepted."
        ),
    )
    horizon.add_argument(
        "--hard-ceiling", action="store_true",
        help=(
            f"Run the global {HARD_RETENTION_MONTHS}-calendar-month ceiling pass "
            f"instead: same planner, older cutoff, and the two retention-class "
            f"artifact exclusions stop protecting"
        ),
    )
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    _load_repo_env(repo_root)
    try:
        identity = load_runtime_identity()
        days = None if args.hard_ceiling else validate_retention_days(args.days)
        bucket = _required(os.environ, "MINIO_BUCKET")
        with coordinated_backup_lock(repo_root / "backups" / ".backup.lock"):
            result = run_prune(connection_factory=_connection_factory(identity), s3_client=_s3_client(),
                               bucket=bucket, identity=identity, retention_days=days,
                               dry_run=bool(args.dry_run),
                               hard_ceiling=bool(args.hard_ceiling))
        print(json.dumps(result, sort_keys=True))
        return 0
    except PlatformPruneError as exc:
        print(json.dumps({"schema": RESULT_SCHEMA, "ok": False, "classification": exc.code,
                          "operator_action_required": exc.operator_action_required}, sort_keys=True), file=sys.stderr)
        return 75 if exc.code in {"PRUNE_LOCKED", "PRUNE_BACKUP_ACTIVE"} else 2


if __name__ == "__main__":
    raise SystemExit(cli())
