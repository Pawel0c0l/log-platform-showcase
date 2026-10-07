#!/usr/bin/env python3
"""Machine-verifiable terminal execution outcome for Telematics `trips_sync`.

WHY THIS EXISTS.
    A business job that returns without doing anything still exits with return
    code 0. The manual-recovery launcher used to read that 0 as proof that the
    business work happened, so a run that hit the disabled-schedule guard and
    returned immediately — zero provider requests, zero pages, zero business
    transaction, zero prepared rows, zero upserted rows — was indistinguishable
    from a genuine committed execution and could advance the coverage watermark.
    (The watermark column is deliberately not named here: this module holds no
    coverage vocabulary, reads no coverage row and issues no coverage SQL.)

    The process exit code is therefore demoted to a *necessary* condition. The
    authoritative statement about what a run actually did is the strictly
    parsed terminal JSON record defined here: exactly one record per business
    process, written once, at the single terminal point of the job.

WHY A FILE AND NOT A TABLE.
    The record must exist even for outcomes that write nothing to the control
    plane (`SKIPPED_DISABLED_SCHEDULE`) and even when the job never reached its
    business database. A file whose path the launcher chooses — the same
    mechanism `LOG_PLATFORM_RUN_ID_FILE` already uses in `ops/runner.py` — needs
    no migration, no new lock order and no new failure mode, and its absence is
    itself a fail-closed signal: a launcher that finds no record refuses.

    The record is evidence, never an authorization. It is trusted only after
    every identity in it has been compared against what the launcher itself
    claimed and against the durable recovery row. See `verify_outcome`.

NOT A LOG CONTRACT.
    Free-form log matching is explicitly not the authoritative channel. Nothing
    in this module parses a human-readable message.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from jobs.api.telematics.request_evidence import (
    WindowCompleteness,
    WindowCompletenessError,
)

#: What a writer emits. Bumped to `/2` by M4, which added `window_completeness`.
EXECUTION_OUTCOME_VERSION = "telematics-trips-execution-outcome/2"

#: The previous shape, which has no `window_completeness` field at all.
EXECUTION_OUTCOME_VERSION_V1 = "telematics-trips-execution-outcome/1"

#: Versions a *reader* accepts, and the exact field set each one has. This is
#: deliberately not "parse leniently and default the rest": each version names
#: its complete field set, and a record is still refused for a missing, extra or
#: wrongly typed field *within* its own version.
#:
#: WHY `/1` IS STILL READ. Records are not only per-launch temporary files.
#: `ops/activate_telematics_trips_schedule.py` re-parses proofs persisted in
#: `client_schedule_run_recovery.job_summary`, some written before M4 existed.
#: Refusing those outright would block the activation of a cold-start chain for
#: a version string, while proving nothing: activation checks committed business
#: work, not §6 condition 6.
#:
#: WHY THAT DOES NOT WEAKEN M4. A `/1` record carries no completeness proof, so
#: `window_completeness` is `None`, and the dispatcher's condition 6 refuses it
#: with `TRIPS_WINDOW_EVIDENCE_ABSENT`. A pre-M4 record therefore remains
#: incapable of advancing a watermark — which is the property that matters —
#: without also becoming unreadable for the paths that never needed it.
SUPPORTED_EXECUTION_OUTCOME_VERSIONS = frozenset({
    EXECUTION_OUTCOME_VERSION_V1,
    EXECUTION_OUTCOME_VERSION,
})

#: Fields added by each version beyond `/1`. Absent from a `/1` payload and
#: required in a `/2` one.
_VERSION_ADDED_FIELDS = {
    EXECUTION_OUTCOME_VERSION_V1: frozenset(),
    EXECUTION_OUTCOME_VERSION: frozenset({"window_completeness"}),
}

#: Environment variable naming the file the business job writes its single
#: terminal record to. Both the reviewed recovery flow and — since M3 — a
#: compatibility `trips_sync` scheduled fire supply a fresh per-launch outcome
#: path; a `strict_meta` fire and an ordinary manual invocation still do not, and
#: the dispatcher strips any inherited value before deciding whether to set its
#: own, so a stale path can never reach a child. The variable is not
#: authenticated — a process running as the same OS user can set it — so it is
#: neither a secret, a capability nor proof of launcher provenance. It selects
#: only where the record is written; the record itself is strictly parsed and
#: identity-checked (see `read_outcome` and `verify_outcome`).
EXECUTION_OUTCOME_FILE_ENV = "TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE"

# --- terminal outcome vocabulary -------------------------------------------
#
# Exactly one of these is written per business process. The two EXECUTED_*
# values are the only ones that may ever advance coverage, and both additionally
# require all three of `provider_execution_entered`,
# `business_transaction_entered` and `transaction_status == COMMITTED`. Those
# are three separate claims — entering the provider, reaching the business
# database, and committing — and a record missing any of them is refused as
# internally contradictory rather than merely treated as non-eligible.
OUTCOME_EXECUTED_COMMITTED = "EXECUTED_COMMITTED"
OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED = "EXECUTED_ZERO_ROWS_COMMITTED"
OUTCOME_SKIPPED_DISABLED_SCHEDULE = "SKIPPED_DISABLED_SCHEDULE"
OUTCOME_SKIPPED_OTHER = "SKIPPED_OTHER"
OUTCOME_FAILED = "FAILED"

TERMINAL_OUTCOMES = frozenset({
    OUTCOME_EXECUTED_COMMITTED,
    OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED,
    OUTCOME_SKIPPED_DISABLED_SCHEDULE,
    OUTCOME_SKIPPED_OTHER,
    OUTCOME_FAILED,
})

#: The only outcomes a coverage finalization gate may accept. A zero-row
#: committed execution is valid work; a skipped zero-work execution is not.
COVERAGE_ELIGIBLE_OUTCOMES = frozenset({
    OUTCOME_EXECUTED_COMMITTED,
    OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED,
})

SKIPPED_OUTCOMES = frozenset({
    OUTCOME_SKIPPED_DISABLED_SCHEDULE,
    OUTCOME_SKIPPED_OTHER,
})

# --- business transaction vocabulary ---------------------------------------
TRANSACTION_NOT_ENTERED = "NOT_ENTERED"
TRANSACTION_COMMITTED = "COMMITTED"
TRANSACTION_NOT_COMMITTED = "NOT_COMMITTED"

TRANSACTION_STATUSES = frozenset({
    TRANSACTION_NOT_ENTERED,
    TRANSACTION_COMMITTED,
    TRANSACTION_NOT_COMMITTED,
})

SKIP_REASON_DISABLED_SCHEDULE = "dataset_schedule_disabled"


class ExecutionOutcomeError(ValueError):
    """A malformed, absent or mismatched terminal record. Never advisory."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        # Retained separately so a caller can render the cause without the code
        # prefix it is already reporting in its own field. `str(self)` is
        # unchanged.
        self.message = message
        super().__init__(f"{code}: {message}")


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_instant(raw: object, *, field: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} is required"
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED",
            f"{field} is not an ISO-8601 instant",
        ) from exc
    if parsed.utcoffset() is None:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} is not timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _require_uuid(raw: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError) as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} is not a canonical UUID"
        ) from exc


def _optional_uuid(raw: object, *, field: str) -> Optional[str]:
    if raw is None or str(raw).strip() == "":
        return None
    return _require_uuid(raw, field=field)


def _require_bool(raw: object, *, field: str) -> bool:
    if not isinstance(raw, bool):
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} must be a JSON boolean"
        )
    return raw


def _require_int(raw: object, *, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} must be a JSON integer"
        )
    if raw < 0:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED", f"{field} must not be negative"
        )
    return raw


@dataclass(frozen=True)
class ExecutionOutcome:
    """One terminal statement about exactly one business process.

    Every field is required. `from_mapping` refuses a record with a missing,
    extra, wrongly typed or internally inconsistent field rather than filling a
    default, because a partially understood record is the exact condition that
    let a skipped run look like a successful one.
    """

    outcome: str
    client_id: str
    client_code: Optional[str]
    schedule_id: Optional[str]
    dataset_name: str
    recovery_run_id: Optional[str]
    platform_run_id: Optional[str]
    requested_window_start_ts: datetime
    requested_window_end_ts: datetime
    provider_execution_entered: bool
    business_transaction_entered: bool
    transaction_status: str
    prepared_count: int
    upserted_count: int
    malformed_count: int
    skipped: bool
    skip_reason: Optional[str]
    terminal_ts: datetime
    #: M4 (`docs/20` §21.2 condition 6). The child's statement about how it
    #: covered its effective window: the tiling it attempted and the terminal
    #: state of every unit in it. `None` for a record that never got far enough
    #: to have one — a skip, an early failure — and that absence is itself a
    #: fail-closed signal, refused by the dispatcher's gate rather than treated
    #: as "nothing to check". It is deliberately NOT rejected at parse time, so
    #: a refusal can name absence and malformation apart.
    window_completeness: Optional[WindowCompleteness] = None
    version: str = EXECUTION_OUTCOME_VERSION

    # -- serialization ------------------------------------------------------

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "version": self.version,
            "outcome": self.outcome,
            "client_id": self.client_id,
            "client_code": self.client_code,
            "schedule_id": self.schedule_id,
            "dataset_name": self.dataset_name,
            "recovery_run_id": self.recovery_run_id,
            "platform_run_id": self.platform_run_id,
            "requested_window_start_ts": _iso(self.requested_window_start_ts),
            "requested_window_end_ts": _iso(self.requested_window_end_ts),
            "provider_execution_entered": self.provider_execution_entered,
            "business_transaction_entered": self.business_transaction_entered,
            "transaction_status": self.transaction_status,
            "prepared_count": self.prepared_count,
            "upserted_count": self.upserted_count,
            "malformed_count": self.malformed_count,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "terminal_ts": _iso(self.terminal_ts),
        }
        # Emit exactly the field set this record's version defines, so a `/1`
        # record read from a persisted `job_summary` round-trips byte-compatibly
        # instead of acquiring a field its own version does not have.
        if "window_completeness" in _VERSION_ADDED_FIELDS.get(
            self.version, frozenset()
        ):
            payload["window_completeness"] = (
                None if self.window_completeness is None
                else self.window_completeness.as_dict()
            )
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.as_dict(), sort_keys=True, ensure_ascii=False,
            separators=(",", ":"),
        )

    # -- strict parsing -----------------------------------------------------

    @classmethod
    def from_mapping(cls, payload: object) -> "ExecutionOutcome":
        if not isinstance(payload, Mapping):
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                "the terminal record is not a JSON object",
            )
        # The version selects the expected field set, so it is read before the
        # field set is checked against it. An unknown version has no field set
        # to check against and is refused outright.
        version = str(payload.get("version") or "")
        if version not in SUPPORTED_EXECUTION_OUTCOME_VERSIONS:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_VERSION_MISMATCH",
                f"terminal record version {version!r} is not one of "
                f"{sorted(SUPPORTED_EXECUTION_OUTCOME_VERSIONS)}",
            )
        known = set(cls.__dataclass_fields__)
        for added_version, added_fields in _VERSION_ADDED_FIELDS.items():
            if added_version != version:
                known -= added_fields
        known |= _VERSION_ADDED_FIELDS[version]
        present = set(payload)
        unknown = sorted(present - known)
        if unknown:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"unknown field(s) in the terminal record: {', '.join(unknown)}",
            )
        missing = sorted(known - present)
        if missing:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"missing field(s) in the terminal record: {', '.join(missing)}",
            )
        outcome = str(payload.get("outcome") or "")
        if outcome not in TERMINAL_OUTCOMES:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"outcome {outcome!r} is not a known terminal outcome",
            )
        transaction_status = str(payload.get("transaction_status") or "")
        if transaction_status not in TRANSACTION_STATUSES:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"transaction_status {transaction_status!r} is not known",
            )

        client_code = payload.get("client_code")
        skip_reason = payload.get("skip_reason")
        raw_completeness = payload.get("window_completeness")
        try:
            completeness = (
                None if raw_completeness is None
                else WindowCompleteness.from_mapping(raw_completeness)
            )
        except WindowCompletenessError as exc:
            # Re-raised in this module's vocabulary because the caller is
            # reading an execution record, not a completeness carrier: a
            # malformed sub-record makes the whole record malformed.
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"window_completeness is invalid: {exc.message}",
            ) from exc
        parsed = cls(
            version=version,
            outcome=outcome,
            client_id=_require_uuid(payload.get("client_id"), field="client_id"),
            client_code=None if client_code is None else str(client_code),
            schedule_id=_optional_uuid(
                payload.get("schedule_id"), field="schedule_id"
            ),
            dataset_name=str(payload.get("dataset_name") or ""),
            recovery_run_id=_optional_uuid(
                payload.get("recovery_run_id"), field="recovery_run_id"
            ),
            platform_run_id=_optional_uuid(
                payload.get("platform_run_id"), field="platform_run_id"
            ),
            requested_window_start_ts=_parse_instant(
                payload.get("requested_window_start_ts"),
                field="requested_window_start_ts",
            ),
            requested_window_end_ts=_parse_instant(
                payload.get("requested_window_end_ts"),
                field="requested_window_end_ts",
            ),
            provider_execution_entered=_require_bool(
                payload.get("provider_execution_entered"),
                field="provider_execution_entered",
            ),
            business_transaction_entered=_require_bool(
                payload.get("business_transaction_entered"),
                field="business_transaction_entered",
            ),
            transaction_status=transaction_status,
            prepared_count=_require_int(
                payload.get("prepared_count"), field="prepared_count"
            ),
            upserted_count=_require_int(
                payload.get("upserted_count"), field="upserted_count"
            ),
            malformed_count=_require_int(
                payload.get("malformed_count"), field="malformed_count"
            ),
            skipped=_require_bool(payload.get("skipped"), field="skipped"),
            skip_reason=None if skip_reason is None else str(skip_reason),
            terminal_ts=_parse_instant(
                payload.get("terminal_ts"), field="terminal_ts"
            ),
            window_completeness=completeness,
        )
        parsed._check_internal_consistency()
        return parsed

    def _check_internal_consistency(self) -> None:
        """Reject records that contradict themselves.

        A forged or buggy record that claims `EXECUTED_COMMITTED` while also
        reporting `skipped=true` or an unentered transaction must not be
        accepted by any gate, so the contradiction is caught at parse time
        rather than left to each consumer.
        """
        if not self.dataset_name:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED", "dataset_name is required"
            )
        if self.requested_window_end_ts < self.requested_window_start_ts:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                "requested_window_end_ts precedes requested_window_start_ts",
            )
        skipped_outcome = self.outcome in SKIPPED_OUTCOMES
        if skipped_outcome != self.skipped:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                f"outcome {self.outcome!r} contradicts skipped={self.skipped}",
            )
        if self.skipped:
            if not self.skip_reason:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    "a skipped record must carry skip_reason",
                )
            if self.business_transaction_entered or \
                    self.transaction_status == TRANSACTION_COMMITTED:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    "a skipped record must not claim a business transaction",
                )
            if self.prepared_count or self.upserted_count:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    "a skipped record must not claim prepared or upserted rows",
                )
            if self.window_completeness is not None:
                # A run that skipped never entered the provider, so it cannot
                # have covered anything. A skip carrying a tiling proof is the
                # same class of self-contradiction as a skip claiming a
                # committed transaction.
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    "a skipped record must not claim window completeness",
                )
        if self.outcome in COVERAGE_ELIGIBLE_OUTCOMES:
            # Provider entry is a *separate* claim from the two below it, and it
            # is the one that says the run actually went out and asked the
            # provider for the window. A record that claims a committed business
            # transaction while also reporting that provider execution was never
            # entered describes an execution that committed rows it cannot have
            # fetched: internally contradictory, and exactly the shape a forged
            # or half-built record takes. It is refused here rather than being
            # left non-eligible downstream, so no consumer has to remember to
            # check it.
            if not self.provider_execution_entered:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    f"{self.outcome} without entered provider execution; a "
                    "committed execution that never entered the provider is "
                    "internally contradictory",
                )
            if not self.business_transaction_entered:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    f"{self.outcome} without an entered business transaction",
                )
            if self.transaction_status != TRANSACTION_COMMITTED:
                raise ExecutionOutcomeError(
                    "EXECUTION_OUTCOME_MALFORMED",
                    f"{self.outcome} with transaction_status "
                    f"{self.transaction_status!r}",
                )
        if self.outcome == OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED and \
                self.upserted_count:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                "EXECUTED_ZERO_ROWS_COMMITTED with a non-zero upserted_count",
            )
        if self.outcome == OUTCOME_EXECUTED_COMMITTED and not self.upserted_count:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                "EXECUTED_COMMITTED with a zero upserted_count; a zero-row "
                "committed execution is reported as "
                "EXECUTED_ZERO_ROWS_COMMITTED",
            )
        if self.transaction_status == TRANSACTION_COMMITTED and \
                not self.business_transaction_entered:
            raise ExecutionOutcomeError(
                "EXECUTION_OUTCOME_MALFORMED",
                "transaction_status COMMITTED without an entered transaction",
            )


# ---------------------------------------------------------------------------
# File channel
# ---------------------------------------------------------------------------

def outcome_path_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """The launcher-chosen record path, or None for an ordinary invocation."""
    source = os.environ if env is None else env
    raw = str(source.get(EXECUTION_OUTCOME_FILE_ENV) or "").strip()
    return Path(raw) if raw else None


def write_outcome(outcome: ExecutionOutcome, *, path: Path) -> None:
    """Write the single terminal record atomically.

    Atomic because a launcher that read a half-written record would see a parse
    failure it could not distinguish from a genuinely malformed one; the rename
    means the file is either absent or complete.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(outcome.to_json() + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_outcome(path: Path) -> ExecutionOutcome:
    """Read and strictly parse the terminal record, or raise."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_ABSENT",
            "the business process wrote no terminal execution record",
        ) from exc
    except OSError as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_UNREADABLE",
            "the terminal execution record could not be read",
        ) from exc
    if not raw.strip():
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_ABSENT",
            "the terminal execution record is empty",
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_MALFORMED",
            "the terminal execution record is not valid JSON",
        ) from exc
    return ExecutionOutcome.from_mapping(payload)


# ---------------------------------------------------------------------------
# Verification against what the launcher itself claimed
# ---------------------------------------------------------------------------

def require_platform_run_identity(
    raw: object, *, field: str, required: bool,
) -> Optional[str]:
    """Canonicalize a platform-run UUID, refusing an absent one when required.

    `platform_run_id` is deliberately optional *in the record*: a job that dies
    before it has loaded its configuration never learns its platform run id, and
    that FAILED record must still be writable. It is **not** optional in a proof
    that is allowed to advance coverage, which is why presence is demanded here,
    by the caller that knows whether coverage is at stake, rather than by the
    parser.
    """
    text = "" if raw is None else str(raw).strip()
    if not text:
        if not required:
            return None
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING",
            f"{field} is absent; execution proof that may advance coverage must "
            "carry an exact platform-run identity",
        )
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError) as exc:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_PLATFORM_RUN_ID_MALFORMED",
            f"{field} is not a canonical UUID",
        ) from exc


def verify_outcome(
    outcome: ExecutionOutcome,
    *,
    client_id: str,
    client_code: Optional[str],
    schedule_id: str,
    dataset_name: str,
    recovery_run_id: Optional[str],
    window_start_ts: datetime,
    window_end_ts: datetime,
    platform_run_id: Optional[str] = None,
) -> None:
    """Prove the record describes *this* execution and no other.

    A record that belongs to another client, schedule, dataset, recovery or
    window — or to a stale execution of the same target — is a mismatch, not a
    success, however well-formed it is.

    `recovery_run_id` is `None` for a scheduled fire, and that is an assertion
    rather than a relaxation: the record must then carry no recovery identity
    either. A manual-recovery record is thereby refused for a scheduled claim
    exactly as a scheduled record is refused for a recovery claim. Passing a
    string behaves as it always has.

    Platform-run identity is required exactly whenever the outcome could advance
    coverage: expected present and valid, observed present and valid, and the two
    equal. A missing value on either side refuses rather than being treated as
    "nothing to compare".
    """
    def _mismatch(field: str, expected: object, observed: object) -> None:
        raise ExecutionOutcomeError(
            "EXECUTION_OUTCOME_IDENTITY_MISMATCH",
            f"terminal record {field} is {observed!r}, expected {expected!r}",
        )

    if outcome.client_id != str(client_id):
        _mismatch("client_id", str(client_id), outcome.client_id)
    if client_code is not None and outcome.client_code != str(client_code):
        _mismatch("client_code", str(client_code), outcome.client_code)
    if outcome.schedule_id != str(schedule_id):
        _mismatch("schedule_id", str(schedule_id), outcome.schedule_id)
    if outcome.dataset_name != str(dataset_name):
        _mismatch("dataset_name", str(dataset_name), outcome.dataset_name)
    expected_recovery_run_id = (
        None if recovery_run_id is None else str(recovery_run_id)
    )
    if outcome.recovery_run_id != expected_recovery_run_id:
        _mismatch(
            "recovery_run_id", expected_recovery_run_id, outcome.recovery_run_id
        )
    if outcome.requested_window_start_ts != window_start_ts.astimezone(timezone.utc):
        _mismatch(
            "requested_window_start_ts", _iso(window_start_ts),
            _iso(outcome.requested_window_start_ts),
        )
    if outcome.requested_window_end_ts != window_end_ts.astimezone(timezone.utc):
        _mismatch(
            "requested_window_end_ts", _iso(window_end_ts),
            _iso(outcome.requested_window_end_ts),
        )
    # Platform-run identity. For a record that may advance coverage this is an
    # exact-match requirement on *both* sides: a falsy value on either side must
    # refuse, never skip the comparison. Skipping it was how a coverage-eligible
    # proof with no platform run id at all could still be accepted.
    coverage_eligible = outcome.outcome in COVERAGE_ELIGIBLE_OUTCOMES
    expected_run_id = require_platform_run_identity(
        platform_run_id,
        field="expected platform_run_id",
        required=coverage_eligible,
    )
    observed_run_id = require_platform_run_identity(
        outcome.platform_run_id,
        field="terminal record platform_run_id",
        required=coverage_eligible,
    )
    if expected_run_id is not None and observed_run_id is not None and \
            expected_run_id != observed_run_id:
        _mismatch("platform_run_id", expected_run_id, observed_run_id)


def is_coverage_eligible(outcome: ExecutionOutcome) -> bool:
    """The structured half of the coverage-advance gate.

    Deliberately not sufficient on its own: the caller must additionally have
    a zero subprocess return code and a verified identity match.

    The four conjuncts are three genuinely different statements plus the skip
    flag, and none implies another: provider execution entered (the run asked
    the provider for the window), business transaction entered (the run reached
    its business database), and the transaction committed. A parsed record can
    no longer contradict any of them — `_check_internal_consistency` refuses
    that at parse time — but they are restated here so a record built in-process,
    without going through `from_mapping`, cannot reach a coverage gate either.
    """
    return (
        outcome.outcome in COVERAGE_ELIGIBLE_OUTCOMES
        and outcome.provider_execution_entered
        and outcome.transaction_status == TRANSACTION_COMMITTED
        and outcome.business_transaction_entered
        and not outcome.skipped
    )


# ---------------------------------------------------------------------------
# Recorder used by the business job
# ---------------------------------------------------------------------------

class ExecutionOutcomeRecorder:
    """Accumulates what a run actually did and writes exactly one record.

    Created unconditionally by the job. When `EXECUTION_OUTCOME_FILE_ENV` is
    absent every method is a no-op, so an ordinary manual or scheduled run is
    byte-identical to its historical behavior.
    """

    def __init__(
        self,
        *,
        params: Mapping[str, Any],
        dataset_name: str,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self._path = outcome_path_from_env(env)
        self._dataset_name = dataset_name
        # Tolerant on purpose: the job validates `params` itself and raises, and
        # that raise must still reach the FAILED record rather than dying here.
        self._params = dict(params) if isinstance(params, Mapping) else {}
        self._written = False
        self._client_id: Optional[str] = None
        self._client_code: Optional[str] = None
        self._schedule_id: Optional[str] = None
        self._platform_run_id: Optional[str] = None
        self._window_start_ts: Optional[datetime] = None
        self._window_end_ts: Optional[datetime] = None
        self._provider_entered = False
        self._transaction_entered = False
        self._transaction_status = TRANSACTION_NOT_ENTERED
        self._prepared = 0
        self._upserted = 0
        self._malformed = 0
        self._window_completeness: Optional[WindowCompleteness] = None

    @property
    def enabled(self) -> bool:
        return self._path is not None

    # -- binding ------------------------------------------------------------

    def bind_target(
        self,
        *,
        client_id: str,
        client_code: Optional[str],
        window_start_ts: datetime,
        window_end_ts: datetime,
        platform_run_id: Optional[str] = None,
    ) -> None:
        self._client_id = str(client_id)
        self._client_code = None if client_code is None else str(client_code)
        self._window_start_ts = window_start_ts
        self._window_end_ts = window_end_ts
        if platform_run_id:
            self._platform_run_id = str(platform_run_id)

    def bind_schedule(self, schedule_id: Optional[str]) -> None:
        self._schedule_id = None if schedule_id is None else str(schedule_id)

    # -- progress markers ---------------------------------------------------

    def mark_provider_entered(self) -> None:
        self._provider_entered = True

    def mark_transaction_entered(self) -> None:
        self._transaction_entered = True
        self._transaction_status = TRANSACTION_NOT_COMMITTED

    def mark_transaction_committed(self) -> None:
        self._transaction_status = TRANSACTION_COMMITTED

    def record_window_completeness(
        self, completeness: Optional[WindowCompleteness]
    ) -> None:
        """Attach the M4 tiling/completeness proof for this execution.

        Called once, after the provider fetch has returned, from the job's own
        collector. It states what the run *observed*; whether that constitutes
        coverage is the dispatcher's decision, made against its own claim.
        """
        self._window_completeness = completeness

    def record_counts(
        self, *, prepared: int, upserted: int, malformed: int
    ) -> None:
        self._prepared = max(0, int(prepared))
        self._upserted = max(0, int(upserted))
        self._malformed = max(0, int(malformed))

    # -- terminal statements ------------------------------------------------

    def record_skipped(self, *, reason: str) -> None:
        outcome = (
            OUTCOME_SKIPPED_DISABLED_SCHEDULE
            if reason == SKIP_REASON_DISABLED_SCHEDULE
            else OUTCOME_SKIPPED_OTHER
        )
        self._emit(outcome, skip_reason=reason)

    def record_executed(self) -> None:
        if not self._provider_entered:
            # Reaching the end of the job without ever having entered provider
            # execution cannot be a committed execution of the window, whatever
            # the transaction did. Emitting one would produce a record the
            # parser refuses as self-contradictory, so the writer refuses first
            # and the two agree.
            self._emit(OUTCOME_FAILED)
            return
        if self._transaction_status != TRANSACTION_COMMITTED:
            # Reaching the end of the job without a committed business
            # transaction is not a success this module is willing to describe
            # as one.
            self._emit(OUTCOME_FAILED)
            return
        self._emit(
            OUTCOME_EXECUTED_COMMITTED if self._upserted
            else OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED
        )

    def record_failed(self) -> None:
        self._emit(OUTCOME_FAILED)

    def _emit(self, outcome: str, *, skip_reason: Optional[str] = None) -> None:
        """Write once. A later statement never overwrites an earlier one."""
        if self._path is None or self._written:
            return
        skipped = outcome in SKIPPED_OUTCOMES
        record = ExecutionOutcome(
            outcome=outcome,
            client_id=self._client_id or str(self._params.get("client_id") or ""),
            client_code=self._client_code,
            schedule_id=self._schedule_id,
            dataset_name=self._dataset_name,
            recovery_run_id=_optional_uuid(
                self._params.get("manual_recovery_run_id"),
                field="manual_recovery_run_id",
            ),
            platform_run_id=self._platform_run_id,
            requested_window_start_ts=(
                self._window_start_ts or datetime.now(timezone.utc)
            ),
            requested_window_end_ts=(
                self._window_end_ts or datetime.now(timezone.utc)
            ),
            provider_execution_entered=self._provider_entered,
            business_transaction_entered=(
                False if skipped else self._transaction_entered
            ),
            transaction_status=(
                TRANSACTION_NOT_ENTERED if skipped else self._transaction_status
            ),
            prepared_count=0 if skipped else self._prepared,
            upserted_count=0 if skipped else self._upserted,
            malformed_count=self._malformed,
            skipped=skipped,
            skip_reason=skip_reason,
            terminal_ts=datetime.now(timezone.utc).replace(microsecond=0),
            # A skip never entered the provider, so it has no tiling to report
            # and the parser refuses one. A FAILED record keeps whatever the run
            # had managed to observe: it cannot advance coverage either way, and
            # a partial tiling is exactly what makes the failure diagnosable.
            window_completeness=None if skipped else self._window_completeness,
        )
        write_outcome(record, path=self._path)
        self._written = True
