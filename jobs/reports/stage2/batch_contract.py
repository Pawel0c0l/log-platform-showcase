from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION = "v1"
STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE = "workflow_b.stage2.cleaned.v1"
STAGE2_ARTIFACT_ROLE = "stage2_cleaned"
STAGE2_LOCK_NAMESPACE = "workflow_b.stage2.raw_file.v1"
STAGE2_PERSISTED_SUCCESS = "OK"


class Stage2Outcome(StrEnum):
    SUCCEEDED_CREATED = "SUCCEEDED_CREATED"
    SUCCEEDED_REUSED = "SUCCEEDED_REUSED"
    SKIPPED_COMPLETED = "SKIPPED_COMPLETED"
    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    UNSUPPORTED_REPORT = "UNSUPPORTED_REPORT"
    AMBIGUOUS_DETECTION = "AMBIGUOUS_DETECTION"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NON_RETRYABLE = "FAILED_NON_RETRYABLE"
    FAILED_IDEMPOTENCY_CONFLICT = "FAILED_IDEMPOTENCY_CONFLICT"
    # P0-C. Reconciliation outcomes, not processing outcomes: they are produced
    # by a read-only sweep over inputs whose durable Stage 2 state leaves them
    # owned by nobody, and they mutate nothing.
    #
    # Every other outcome above describes what this batch just did to a file.
    # These two describe the durable state a file was left in — by an earlier
    # batch or by this one — that Stage 2 discovery will never re-pick and
    # Stage 3 discovery will never consume. Without them such a file is
    # reported once, as an ordinary success, and is then silent forever.
    STRANDED_AWAITING_REVIEW = "STRANDED_AWAITING_REVIEW"
    STRANDED_UNROUTABLE = "STRANDED_UNROUTABLE"


# Outcomes that report a durable state rather than an action taken on it. They
# are excluded from `attempted_count` for the same reason SKIPPED_ outcomes
# are: the sweep that produces them attempts nothing. A file this batch did
# process and then stranded is deliberately counted here rather than as a
# Stage 2 success — being counted as a success is what hid the state.
STAGE2_RECONCILIATION_OUTCOMES = frozenset({
    Stage2Outcome.STRANDED_AWAITING_REVIEW,
    Stage2Outcome.STRANDED_UNROUTABLE,
})

# Technical/integrity failures. These already stop the batch (`has_batch_failures`
# raises `Stage2BatchError`) and already reach the orchestrator as FAILED_*.
STAGE2_FAILURE_OUTCOMES = frozenset({
    Stage2Outcome.FAILED_RETRYABLE,
    Stage2Outcome.FAILED_NON_RETRYABLE,
    Stage2Outcome.FAILED_IDEMPOTENCY_CONFLICT,
})


@dataclass(slots=True)
class Stage2ItemResult:
    raw_file_id: str
    outcome: Stage2Outcome
    persisted_status: str | None = None
    reason_code: str | None = None
    retryable: bool = False
    review_required: bool = False
    client_code: str | None = None
    report_type: str | None = None
    artifact_id: str | None = None
    artifact_idempotency_status: str = "none"
    idempotency_digest_short: str | None = None
    source_identity: str | None = None
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


@dataclass(slots=True)
class Stage2BatchResult:
    stage_name: str = "workflow_b.stage2"
    discovered_candidate_count: int = 0
    eligible_count: int = 0
    items: list[Stage2ItemResult] = field(default_factory=list)

    def count(self, outcome: Stage2Outcome) -> int:
        return sum(item.outcome == outcome for item in self.items)

    @property
    def attempted_count(self) -> int:
        return sum(
            not item.outcome.value.startswith("SKIPPED_")
            and item.outcome not in STAGE2_RECONCILIATION_OUTCOMES
            for item in self.items
        )

    @property
    def stranded_count(self) -> int:
        return sum(item.outcome in STAGE2_RECONCILIATION_OUTCOMES for item in self.items)

    @property
    def retryable_work_remains(self) -> bool:
        return any(item.retryable for item in self.items)

    @property
    def operator_action_required(self) -> bool:
        return any(
            item.review_required
            or item.outcome in {
                Stage2Outcome.FAILED_NON_RETRYABLE,
                Stage2Outcome.FAILED_IDEMPOTENCY_CONFLICT,
            }
            for item in self.items
        )

    def to_dict(self) -> dict[str, Any]:
        mapping = {
            "succeeded_created_count": Stage2Outcome.SUCCEEDED_CREATED,
            "succeeded_reused_count": Stage2Outcome.SUCCEEDED_REUSED,
            "skipped_completed_count": Stage2Outcome.SKIPPED_COMPLETED,
            "skipped_ineligible_count": Stage2Outcome.SKIPPED_INELIGIBLE,
            "skipped_locked_count": Stage2Outcome.SKIPPED_LOCKED,
            "validation_rejected_count": Stage2Outcome.REJECTED_VALIDATION,
            "pending_review_count": Stage2Outcome.PENDING_HUMAN_REVIEW,
            "unsupported_count": Stage2Outcome.UNSUPPORTED_REPORT,
            "ambiguous_count": Stage2Outcome.AMBIGUOUS_DETECTION,
            "retryable_failure_count": Stage2Outcome.FAILED_RETRYABLE,
            "non_retryable_failure_count": Stage2Outcome.FAILED_NON_RETRYABLE,
            "idempotency_conflict_count": Stage2Outcome.FAILED_IDEMPOTENCY_CONFLICT,
            "stranded_awaiting_review_count": Stage2Outcome.STRANDED_AWAITING_REVIEW,
            "stranded_unroutable_count": Stage2Outcome.STRANDED_UNROUTABLE,
        }
        out = {
            "stage_name": self.stage_name,
            "discovered_candidate_count": self.discovered_candidate_count,
            "eligible_count": self.eligible_count,
            "attempted_count": self.attempted_count,
            "stranded_count": self.stranded_count,
            "artifact_created_count": self.count(Stage2Outcome.SUCCEEDED_CREATED),
            "artifact_reused_count": self.count(Stage2Outcome.SUCCEEDED_REUSED),
            "retryable_work_remains": self.retryable_work_remains,
            "operator_action_required": self.operator_action_required,
            "items": [item.to_dict() for item in self.items],
        }
        out.update({name: self.count(outcome) for name, outcome in mapping.items()})
        return out


class Stage2BatchError(RuntimeError):
    def __init__(self, result: Stage2BatchResult):
        self.result = result
        counts = result.to_dict()
        super().__init__(
            "Stage 2 batch contained technical/integrity failures "
            f"(retryable={counts['retryable_failure_count']}, "
            f"non_retryable={counts['non_retryable_failure_count']}, "
            f"idempotency_conflicts={counts['idempotency_conflict_count']})"
        )

    @property
    def partial_result(self) -> Stage2BatchResult:
        return self.result


def cleaned_artifact_idempotency_key(
    raw_file_id: str,
    source_identity: str,
    *,
    contract_version: str = STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION,
) -> str:
    payload = json.dumps(
        ["workflow_b.stage2.cleaned", contract_version, str(raw_file_id), str(source_identity)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stage2_advisory_lock_key(raw_file_id: str) -> int:
    digest = hashlib.sha256(
        f"{STAGE2_LOCK_NAMESPACE}\0{raw_file_id}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def is_retryable_historical_state(status: str | None, reason: str | None) -> bool:
    return status == "PENDING_REVIEW" and reason == "stage2_exception"


def has_batch_failures(result: Stage2BatchResult) -> bool:
    return any(item.outcome in STAGE2_FAILURE_OUTCOMES for item in result.items)


def item_carries_operator_ownership(item: Stage2ItemResult) -> bool:
    """True when this item already makes its file operator-visible for the cycle.

    P0-C. The reconciliation sweep uses this to decide whether a file it found
    durably unowned is *already* accounted for by the batch that just ran.

    Merely appearing in `Stage2BatchResult.items` is not enough. A file this
    batch processed to `stage2_status='OK'` with no `client_code` is appended as
    `SUCCEEDED_CREATED` with `review_required=False` — an ordinary success that
    no stage will ever pick up again. Skipping it because its id is present
    would defer operator visibility to the next 06:00/20:00 cycle.

    A failure outcome does own the file even with `review_required=False`: it
    already raises `Stage2BatchError` and reaches the orchestrator as FAILED_*,
    which is a strictly louder signal than a review item. Overwriting it would
    silence `has_batch_failures`.
    """
    return (
        item.review_required
        or item.outcome in STAGE2_RECONCILIATION_OUTCOMES
        or item.outcome in STAGE2_FAILURE_OUTCOMES
    )
