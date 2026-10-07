"""Stage 3 crash/failure recovery classification (P0-E).

WHY THIS EXISTS.
    `_select_stage3_candidates` admitted a file only when `stage3_status` was
    NULL, empty, or a superseded 'OK'. Every other durable value it can hold —
    'RUNNING' from `_mark_stage3_started`, 'ERROR' from `_mark_stage3_error` —
    was therefore terminal *by omission*: nothing selected the file again, ever.
    A process killed one millisecond after `_mark_stage3_started` committed
    removed the file from the autonomous system permanently.

THE THREE THINGS A RECOVERY DECISION MUST NOT DO.
    Skip a required newer load. Duplicate a committed one. Steal a file from a
    process that is still legitimately working on it. Each has its own
    mechanism here, because no single signal covers all three.

1. ATTEMPT IDENTITY IS CONTENT IDENTITY, NOT RAW-FILE IDENTITY.
    Counting destination rows for a `raw_file_id` proves only that *some* load
    of that file once committed. It cannot distinguish this:

        load succeeds -> Stage 2 re-cleans the file -> new attempt stamps
        RUNNING -> process dies before the new destination transaction commits

    The old rows are still there. Row-count-by-raw-file would call that
    "committed", finalize the platform row OK, and the newer cleaned generation
    would never be loaded — silent data loss.

    The discriminator is the **cleaned artifact** each destination row records.
    A re-clean produces a *new* artifact id, so rows from the previous
    generation carry the previous id and can never be mistaken for the current
    attempt's work. This needs no clock comparison between two databases and no
    new column: every current loader already writes it.

2. PROVENANCE COLUMNS DIFFER BY LOAD STRATEGY.
    There is no universal `_raw_file_id`. Verified against production:

      * `telematics_reports.*` (report_207, report_d105_2_ecodriving) — written by
        `_insert_rows` / `_upsert_record_id_rows` / `_replace_table_rows`, carry
        `_raw_file_id`, `_source_artifact_id`, `_stage3_run_id`, `_loaded_at`;
      * `telematics_reports."Alpha_GPS_Baza_LOG"` — written by
        `_replace_alpha_gps_rows`, carries `raw_file_id`, `cleaned_artifact_id`,
        `workflow_run_id`, `imported_at`, and **no** underscore-prefixed column.
        44 production Stage 3 OK files use it.

    A probe hard-coded to `_raw_file_id` therefore declares every interrupted
    Alpha GPS load uninspectable and red-cycles Workflow B twice a day instead
    of restoring ownership. Provenance is resolved per strategy below.

3. LIVENESS IS A LOCK, NOT A TIMEOUT.
    `log-workflow-b.service` allows `TimeoutStartSec=6h`, `log-job@.service` 4h,
    and a standalone `ops/runner.py` invocation of Stage 3 is unbounded and does
    not take the orchestration lock. No wall-clock threshold can therefore prove
    that no legitimate owner is still alive.

    So ownership is claimed explicitly: Stage 3 takes a per-raw-file advisory
    lock before it touches the row (`stage3_file_advisory_lock_key`). A
    PostgreSQL session-level advisory lock is released by the server when the
    session ends, so a crashed process stops owning its file the moment it dies,
    and a live one keeps owning it for as long as it runs — however long that
    is. Recovery may only proceed when it can take that lock.

    The age threshold that remains is defence in depth, not the authority, and
    is set above every supported systemd bound rather than tuned.

WHY NO NEW COLUMN.
    Every signal above already exists durably. A `stage3_recovery_state` column
    would add a second, weaker source of truth that the same crash could leave
    disagreeing with the destination — the failure mode this module removes.

READ-ONLY.
    Every statement here is a SELECT or a lock probe. This module classifies; it
    never marks, loads or repairs.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

#: Defence in depth only — the advisory lock is the ownership authority. Set
#: above every supported execution bound so it can never be the thing that
#: steals a live load: `log-workflow-b.service` TimeoutStartSec=6h,
#: `log-job@.service` 4h. 480 minutes clears the largest of those with margin
#: and still sits below the 10-14 hour gap between the 06:00 and 20:00 fires,
#: so an orphan is recovered on the next cycle rather than a day later.
DEFAULT_STAGE3_STALE_GRACE_MINUTES = 480

#: Durable values `stage3_status` can hold; nothing else writes it.
STATUS_RUNNING = "RUNNING"
STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
STATUS_SKIPPED_NO_RECORD_ID = "SKIPPED_NO_RECORD_ID"


class Stage3LoadStrategy(StrEnum):
    """How a report type's rows reach the client business database.

    Recovery behaviour is a property of the strategy, not of the report type, so
    every strategy must declare its provenance columns and whether replaying an
    uncommitted attempt is safe.
    """

    #: `telematics_reports.<report_type>` via insert / record_id upsert /
    #: replace-table. Underscore-prefixed technical columns.
    TELEMATICS_TECHNICAL = "telematics_technical"
    #: `telematics_reports."Alpha_GPS_Baza_LOG"` via DELETE + INSERT of the whole
    #: workbook. Bare provenance column names.
    ALPHA_GPS_REPLACE_ALL = "alpha_gps_replace_all"


@dataclass(frozen=True, slots=True)
class DestinationProvenance:
    """Where a strategy records which file and which cleaned artifact it loaded."""

    raw_file_column: str
    cleaned_artifact_column: str
    run_column: str
    loaded_at_column: str
    #: Whether re-running an attempt that did not commit can duplicate business
    #: data. False here is a *proven* property of the writer, not an assumption:
    #:
    #:   * TELEMATICS_TECHNICAL — `_upsert_record_id_rows` upserts on the unique
    #:     `record_id` index, `_insert_rows` uses ON CONFLICT DO NOTHING, and
    #:     `_replace_table_rows` replaces wholesale. All three converge.
    #:   * ALPHA_GPS_REPLACE_ALL — `_replace_alpha_gps_rows` deletes the table
    #:     and reinserts the parsed workbook, so a second run reaches the same
    #:     state as the first.
    replay_is_idempotent: bool
    #: Whether a committed attempt can legitimately leave zero rows. Both
    #: current strategies can: Alpha GPS commits a bare DELETE when the
    #: workbook parses to no rows, and the telematics insert path commits when
    #: every row was filtered. So a zero count NEVER proves "did not commit" —
    #: it only means the safe action is decided by `replay_is_idempotent`.
    zero_row_commit_possible: bool


DESTINATION_PROVENANCE: dict[Stage3LoadStrategy, DestinationProvenance] = {
    Stage3LoadStrategy.TELEMATICS_TECHNICAL: DestinationProvenance(
        raw_file_column="_raw_file_id",
        cleaned_artifact_column="_source_artifact_id",
        run_column="_stage3_run_id",
        loaded_at_column="_loaded_at",
        replay_is_idempotent=True,
        zero_row_commit_possible=True,
    ),
    Stage3LoadStrategy.ALPHA_GPS_REPLACE_ALL: DestinationProvenance(
        raw_file_column="raw_file_id",
        cleaned_artifact_column="cleaned_artifact_id",
        run_column="workflow_run_id",
        loaded_at_column="imported_at",
        replay_is_idempotent=True,
        zero_row_commit_possible=True,
    ),
}


class Stage3RecoveryClass(StrEnum):
    """What the *next owner* of this durable state must do."""

    #: Ordinary first attempt, or an 'OK' row Stage 2 superseded. Not recovery.
    NOT_RECOVERY = "NOT_RECOVERY"
    #: Deterministic evidence that the current attempt did not commit, and the
    #: strategy makes replaying it safe.
    SAFE_REPLAY = "SAFE_REPLAY"
    #: Deterministic attempt-scoped evidence that THIS attempt committed.
    #: Reconcile the platform row; never load again.
    RECONCILE_COMMITTED = "RECONCILE_COMMITTED"
    #: A live process holds this file's ownership lock. Not ours to touch.
    AWAIT_LIVE_OWNER = "AWAIT_LIVE_OWNER"
    #: Non-terminal but still inside the secondary age bound.
    AWAIT_GRACE = "AWAIT_GRACE"
    #: Terminal by design, unclassifiable, evidence unreadable, or a failure
    #: durably known to be non-retryable. Owner is an operator.
    TERMINAL_OPERATOR = "TERMINAL_OPERATOR"


@dataclass(frozen=True, slots=True)
class DestinationEvidence:
    """Attempt-scoped answer to "did the current attempt commit?"."""

    #: Rows recording the cleaned artifact the current attempt would load. This
    #: is the attempt-scoped signal.
    rows_for_current_attempt: int | None
    #: Rows for the raw file regardless of generation. Diagnostic only — it must
    #: never drive the decision, because that is exactly the supersede bug.
    rows_for_raw_file: int | None
    strategy: Stage3LoadStrategy | None
    unreadable_reason: str | None = None

    @property
    def readable(self) -> bool:
        return self.rows_for_current_attempt is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_for_current_attempt": self.rows_for_current_attempt,
            "rows_for_raw_file": self.rows_for_raw_file,
            "strategy": self.strategy.value if self.strategy else None,
            "unreadable_reason": self.unreadable_reason,
        }


@dataclass(frozen=True, slots=True)
class Stage3RecoveryDecision:
    recovery_class: Stage3RecoveryClass
    reason: str
    stale_grace_minutes: int | None = None
    stale_deadline: datetime | None = None
    evidence: DestinationEvidence | None = None
    error_category: str | None = None

    @property
    def is_recovery(self) -> bool:
        return self.recovery_class in {
            Stage3RecoveryClass.SAFE_REPLAY,
            Stage3RecoveryClass.RECONCILE_COMMITTED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "recovery_class": self.recovery_class.value,
            "reason": self.reason,
            "stale_grace_minutes": self.stale_grace_minutes,
            "stale_deadline": (
                self.stale_deadline.isoformat() if self.stale_deadline else None
            ),
            "error_category": self.error_category,
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


# --------------------------------------------------------------------------- #
# Durable retryability of a failed attempt
# --------------------------------------------------------------------------- #

#: `_mark_stage3_error` prefixes `stage3_error` with this, so the retryability
#: the batch classifier already computes in memory survives the process that
#: computed it. Without it, every ERROR looks alike after a restart, and a
#: deterministic malformed-data failure would be retried at 06:00 and 20:00
#: forever — red-cycling Workflow B twice a day with no path to resolution.
#:
#: Chosen over a new column because it is exactly as durable, and over a
#: distinct `stage3_status` value because operator tooling greps for 'ERROR'.
STAGE3_ERROR_EVIDENCE_RE = re.compile(
    r"^\[stage3 category=(?P<category>[a-z0-9_]+) retry=(?P<retry>yes|no)\]\s*"
)


def format_stage3_error_evidence(category: str, *, retryable: bool) -> str:
    safe = re.sub(r"[^a-z0-9_]+", "_", str(category or "unclassified").lower()).strip("_")
    return f"[stage3 category={safe or 'unclassified'} retry={'yes' if retryable else 'no'}] "


def parse_stage3_error_evidence(stage3_error: str | None) -> tuple[str | None, bool | None]:
    """Recover (category, retryable) from a durable ERROR row.

    Returns (None, None) when the marker is absent or malformed — which is the
    fail-closed answer for a row written before this contract existed, or by
    anything other than `_mark_stage3_error`. Such a row gets operator
    ownership rather than a guessed retry. Production currently holds zero
    `ERROR` rows, so there is no legacy backlog this strands.
    """
    match = STAGE3_ERROR_EVIDENCE_RE.match(str(stage3_error or ""))
    if not match:
        return None, None
    return match.group("category"), match.group("retry") == "yes"


# --------------------------------------------------------------------------- #
# Per-file ownership
# --------------------------------------------------------------------------- #

STAGE3_FILE_LOCK_NAMESPACE = "workflow_b.stage3.file.v1"


def stage3_file_advisory_lock_key(raw_file_id: str) -> int:
    """A stable signed-64-bit advisory key for one raw file.

    Namespaced so it cannot collide with `workflow_b_advisory_lock_key()`, which
    guards the whole orchestration cycle rather than one file.
    """
    digest = hashlib.sha256(
        f"{STAGE3_FILE_LOCK_NAMESPACE}:{raw_file_id}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def is_stale_running(
    *,
    stage3_started_at: datetime | None,
    now: datetime,
    stale_grace_minutes: int = DEFAULT_STAGE3_STALE_GRACE_MINUTES,
) -> tuple[bool, datetime | None]:
    """Secondary age bound. The lock, not this, decides liveness.

    A RUNNING row with no `stage3_started_at` is treated as past the bound: the
    column is written in the same statement as the status, so its absence means
    no window can be computed, and refusing to recover would recreate the
    permanent orphan.
    """
    if stage3_started_at is None:
        return True, None
    deadline = stage3_started_at + timedelta(minutes=stale_grace_minutes)
    return now >= deadline, deadline


def classify_stage3_recovery(
    *,
    stage3_status: str | None,
    stage3_started_at: datetime | None,
    stage3_error: str | None = None,
    now: datetime,
    evidence: DestinationEvidence | None,
    file_lock_acquired: bool = True,
    stale_grace_minutes: int = DEFAULT_STAGE3_STALE_GRACE_MINUTES,
) -> Stage3RecoveryDecision:
    """Decide the next owner of one durable Stage 3 state.

    `file_lock_acquired` is the liveness authority: False means a live process
    holds this file and nothing else may act on it, whatever the clock says.
    """
    status = str(stage3_status or "").strip().upper()

    if status in {"", STATUS_OK}:
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.NOT_RECOVERY,
            "ordinary eligibility: never attempted, or superseded by a newer Stage 2 result",
        )

    if not file_lock_acquired:
        # Checked before anything else that could act on the row. A live owner
        # outranks every other consideration, including an expired age bound —
        # a legitimate execution may run for hours.
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.AWAIT_LIVE_OWNER,
            "another process holds this file's Stage 3 ownership lock",
        )

    if status == STATUS_SKIPPED_NO_RECORD_ID:
        # A deliberate refusal to load ambiguous rows, not an interruption.
        # Replaying it would refuse identically.
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.TERMINAL_OPERATOR,
            "SKIPPED_NO_RECORD_ID is a deliberate data refusal; retry cannot change the outcome",
        )

    if status == STATUS_RUNNING:
        stale, deadline = is_stale_running(
            stage3_started_at=stage3_started_at,
            now=now,
            stale_grace_minutes=stale_grace_minutes,
        )
        if not stale:
            # The lock was free, so no live owner exists — but the row is young
            # enough that a process may be starting up between claiming the row
            # and taking the lock. Standing aside costs one cycle and removes
            # that race entirely.
            return Stage3RecoveryDecision(
                Stage3RecoveryClass.AWAIT_GRACE,
                "RUNNING within its secondary age bound",
                stale_grace_minutes=stale_grace_minutes,
                stale_deadline=deadline,
                evidence=evidence,
            )
        return _decide_from_evidence(
            evidence,
            stale_grace_minutes=stale_grace_minutes,
            stale_deadline=deadline,
            interrupted_reason="RUNNING beyond its age bound with no live owner",
        )

    if status == STATUS_ERROR:
        # Committed work outranks retryability: an exception raised *after* the
        # destination commit (an artifact upload, a finalization) also lands
        # here, so the destination is consulted before the error marker.
        if evidence is not None and evidence.readable and evidence.rows_for_current_attempt:
            return _decide_from_evidence(
                evidence,
                stale_grace_minutes=None,
                stale_deadline=None,
                interrupted_reason="ERROR raised after this attempt committed",
            )
        category, retryable = parse_stage3_error_evidence(stage3_error)
        if retryable is None:
            return Stage3RecoveryDecision(
                Stage3RecoveryClass.TERMINAL_OPERATOR,
                "ERROR carries no durable retryability evidence; classification is an operator decision",
                evidence=evidence,
                error_category=category,
            )
        if not retryable:
            # The distinction cadence alone cannot make. A deterministic
            # malformed-data or configuration failure would otherwise fail
            # identically at 06:00 and 20:00 forever.
            return Stage3RecoveryDecision(
                Stage3RecoveryClass.TERMINAL_OPERATOR,
                f"ERROR is durably classified non-retryable ({category}); autonomous retry cannot resolve it",
                evidence=evidence,
                error_category=category,
            )
        return _decide_from_evidence(
            evidence,
            stale_grace_minutes=None,
            stale_deadline=None,
            interrupted_reason=f"ERROR durably classified retryable ({category})",
            error_category=category,
        )

    return Stage3RecoveryDecision(
        Stage3RecoveryClass.TERMINAL_OPERATOR,
        f"unrecognized stage3_status {status!r}; operator classification required",
        evidence=evidence,
    )


def _decide_from_evidence(
    evidence: DestinationEvidence | None,
    *,
    stale_grace_minutes: int | None,
    stale_deadline: datetime | None,
    interrupted_reason: str,
    error_category: str | None = None,
) -> Stage3RecoveryDecision:
    if evidence is None or not evidence.readable:
        detail = (evidence.unreadable_reason if evidence else None) or "destination not inspected"
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.TERMINAL_OPERATOR,
            f"{interrupted_reason}, but attempt-scoped destination evidence is unavailable "
            f"({detail}); neither replay nor reconciliation is safe",
            stale_grace_minutes=stale_grace_minutes,
            stale_deadline=stale_deadline,
            evidence=evidence,
            error_category=error_category,
        )

    if evidence.rows_for_current_attempt:
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.RECONCILE_COMMITTED,
            f"{interrupted_reason}; destination rows carry this attempt's cleaned artifact",
            stale_grace_minutes=stale_grace_minutes,
            stale_deadline=stale_deadline,
            evidence=evidence,
            error_category=error_category,
        )

    provenance = DESTINATION_PROVENANCE.get(evidence.strategy) if evidence.strategy else None
    if provenance is None or not provenance.replay_is_idempotent:
        # Zero attempt-scoped rows means the load either did not commit or
        # committed empty. Both are resolved by replaying — but only where the
        # writer is known to converge. An undeclared strategy fails closed
        # rather than inheriting that property.
        return Stage3RecoveryDecision(
            Stage3RecoveryClass.TERMINAL_OPERATOR,
            f"{interrupted_reason}; no rows carry this attempt's cleaned artifact, and this "
            f"load strategy does not declare replay as idempotent",
            stale_grace_minutes=stale_grace_minutes,
            stale_deadline=stale_deadline,
            evidence=evidence,
            error_category=error_category,
        )

    stale_generation = bool(evidence.rows_for_raw_file)
    detail = (
        "only rows from a previous cleaned generation exist, so the required newer load is still owed"
        if stale_generation
        else "no rows carry this attempt's cleaned artifact"
    )
    return Stage3RecoveryDecision(
        Stage3RecoveryClass.SAFE_REPLAY,
        f"{interrupted_reason}; {detail}, and this strategy's writer is idempotent on replay",
        stale_grace_minutes=stale_grace_minutes,
        stale_deadline=stale_deadline,
        evidence=evidence,
        error_category=error_category,
    )


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #

def probe_destination_evidence(
    destination_conn,
    *,
    strategy: Stage3LoadStrategy | None,
    destination_schema: str,
    destination_table: str,
    raw_file_id: str,
    cleaned_artifact_id: str | None,
) -> DestinationEvidence:
    """Attempt-scoped, read-only, per-strategy destination inspection.

    Returns counts, or `unreadable_reason` when the question cannot be answered.
    Unreadable is deliberately distinct from zero: the classifier replays on
    zero and refuses on unreadable, and collapsing the two would turn "I could
    not check" into "it is safe to load again".

    Identifiers reach SQL only through `psycopg.sql.Identifier`, and both column
    names come from the static `DESTINATION_PROVENANCE` table rather than from
    any row value.
    """
    if strategy is None:
        return DestinationEvidence(None, None, None, "no load strategy resolved for this report type")
    provenance = DESTINATION_PROVENANCE.get(strategy)
    if provenance is None:
        return DestinationEvidence(None, None, strategy, f"strategy {strategy.value} declares no provenance")
    if not destination_schema or not destination_table or not raw_file_id:
        return DestinationEvidence(None, None, strategy, "destination or raw file identity is unknown")
    if not cleaned_artifact_id:
        # Without the current cleaned artifact there is no attempt-scoped
        # question to ask, only the raw-file question that caused the defect.
        return DestinationEvidence(
            None, None, strategy, "current cleaned artifact id is unavailable"
        )

    from psycopg import sql

    def _scalar(row) -> int:
        return int(row["n"] if isinstance(row, dict) else row[0])

    with destination_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                bool_or(column_name = %s) AS has_raw,
                bool_or(column_name = %s) AS has_cleaned
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (
                provenance.raw_file_column,
                provenance.cleaned_artifact_column,
                destination_schema,
                destination_table,
            ),
        )
        row = cur.fetchone()
        if row is None:
            return DestinationEvidence(None, None, strategy, "destination table not found")
        has_raw = bool(row["has_raw"] if isinstance(row, dict) else row[0])
        has_cleaned = bool(row["has_cleaned"] if isinstance(row, dict) else row[1])
        if not has_raw or not has_cleaned:
            return DestinationEvidence(
                None,
                None,
                strategy,
                f"destination lacks {provenance.raw_file_column!r}/"
                f"{provenance.cleaned_artifact_column!r} provenance",
            )

        table = sql.SQL("{}.{}").format(
            sql.Identifier(destination_schema), sql.Identifier(destination_table)
        )
        raw_col = sql.Identifier(provenance.raw_file_column)
        cleaned_col = sql.Identifier(provenance.cleaned_artifact_column)

        cur.execute(
            sql.SQL("SELECT count(*) AS n FROM {} WHERE {}::text = %s").format(table, raw_col),
            (str(raw_file_id),),
        )
        rows_for_raw_file = _scalar(cur.fetchone())

        cur.execute(
            sql.SQL(
                "SELECT count(*) AS n FROM {} WHERE {}::text = %s AND {}::text = %s"
            ).format(table, raw_col, cleaned_col),
            (str(raw_file_id), str(cleaned_artifact_id)),
        )
        rows_for_current_attempt = _scalar(cur.fetchone())

    return DestinationEvidence(
        rows_for_current_attempt=rows_for_current_attempt,
        rows_for_raw_file=rows_for_raw_file,
        strategy=strategy,
    )
