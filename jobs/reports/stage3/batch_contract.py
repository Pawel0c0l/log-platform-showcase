from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Stage3Outcome(StrEnum):
    LOADED = "LOADED"
    # P0-E. An interrupted attempt whose destination load is durably present.
    # Distinct from LOADED because nothing was written this cycle: the platform
    # row was finalized from the destination's own evidence. It still yields a
    # successful-load identity, because the postprocessor step the crash
    # skipped genuinely is still owed to this file.
    RECOVERED_RECONCILED = "RECOVERED_RECONCILED"
    DRY_RUN_SUCCEEDED = "DRY_RUN_SUCCEEDED"
    SKIPPED_COMPLETED = "SKIPPED_COMPLETED"
    # P0-E. Non-terminal but still inside its window — another process owns it.
    # A skip, not a failure: nothing is wrong and nothing is owed to an operator.
    SKIPPED_RECOVERY_DEFERRED = "SKIPPED_RECOVERY_DEFERRED"
    # P0-E. Terminal by design, unclassifiable, or destination evidence
    # unreadable. Never silently dropped: reported every cycle until resolved.
    BLOCKED_RECOVERY_OPERATOR = "BLOCKED_RECOVERY_OPERATOR"
    SKIPPED_INELIGIBLE = "SKIPPED_INELIGIBLE"
    SKIPPED_ROUTING = "SKIPPED_ROUTING"
    SKIPPED_LIMIT = "SKIPPED_LIMIT"
    REJECTED_VALIDATION = "REJECTED_VALIDATION"
    FAILED_RETRYABLE_DATABASE = "FAILED_RETRYABLE_DATABASE"
    FAILED_RETRYABLE_INFRASTRUCTURE = "FAILED_RETRYABLE_INFRASTRUCTURE"
    FAILED_NON_RETRYABLE_CONFIGURATION = "FAILED_NON_RETRYABLE_CONFIGURATION"
    FAILED_ENVIRONMENT_IDENTITY = "FAILED_ENVIRONMENT_IDENTITY"
    FAILED_PERMISSION = "FAILED_PERMISSION"
    FAILED_SCHEMA_NOT_READY = "FAILED_SCHEMA_NOT_READY"
    FAILED_DATABASE_LOAD = "FAILED_DATABASE_LOAD"
    # P0-E. A Stage 3 writer or validator deterministically refused its input:
    # an empty or malformed workbook, invalid columns, a duplicate record_id
    # that blocks the unique index, an unusable artifact, or a missing client
    # account. Separated from FAILED_DATABASE_LOAD because that one is the
    # retryable bucket, and repeating an unchanged input cannot fix any of these.
    FAILED_WRITER_VALIDATION = "FAILED_WRITER_VALIDATION"
    BLOCKED_OPERATOR_ACTION = "BLOCKED_OPERATOR_ACTION"


@dataclass(frozen=True, slots=True)
class Stage3SuccessfulLoadIdentity:
    raw_file_id: str
    client_code: str
    report_type: str
    destination_schema: str
    destination_table: str
    source_cleaned_artifact_id: str | None
    final_status: str
    #: P0-E. True when this identity was reconstructed by crash recovery rather
    #: than produced by a load performed in this cycle. The orchestrator uses it
    #: to refuse re-offering a postprocessor whose replay safety is not declared
    #: — a fresh load has no such doubt, a reconciled one does.
    recovered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Stage3ItemResult:
    raw_file_id: str
    client_code: str | None
    report_type: str | None
    outcome: Stage3Outcome
    destination_schema: str | None = None
    destination_table: str | None = None
    persisted_status: str | None = None
    error_category: str | None = None
    retryable: bool = False
    operator_action_required: bool = False
    error_detail: str | None = None
    dry_run: bool = False
    force_reprocess: bool = False
    inserted_rows: int = 0
    updated_rows: int = 0
    skipped_rows: int = 0
    rejected_rows: int = 0
    source_cleaned_artifact_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


@dataclass(slots=True)
class Stage3BatchResult:
    stage_name: str = "workflow_b.stage3"
    discovered_candidate_count: int = 0
    eligible_count: int = 0
    items: list[Stage3ItemResult] = field(default_factory=list)

    def count(self, outcome: Stage3Outcome) -> int:
        return sum(item.outcome == outcome for item in self.items)

    @property
    def attempted_count(self) -> int:
        return sum(not item.outcome.value.startswith("SKIPPED_") for item in self.items)

    @property
    def successful_load_identities(self) -> list[Stage3SuccessfulLoadIdentity]:
        return [
            Stage3SuccessfulLoadIdentity(
                raw_file_id=item.raw_file_id,
                client_code=str(item.client_code),
                report_type=str(item.report_type),
                destination_schema=str(item.destination_schema),
                destination_table=str(item.destination_table),
                source_cleaned_artifact_id=item.source_cleaned_artifact_id,
                final_status=str(item.persisted_status),
                recovered=item.outcome == Stage3Outcome.RECOVERED_RECONCILED,
            )
            for item in self.items
            # P0-E. RECOVERED_RECONCILED joins LOADED here on purpose: the
            # crash that stranded the file happened somewhere between the
            # destination commit and the postprocessor, so the postprocessor may
            # never have run, and re-offering the identity is what closes crash
            # windows E-H.
            #
            # This is NOT a claim that postprocessors are convergent — report
            # 207 is not; it increments counters and is safe only because its
            # `migrated_to_client_db` marker and the increment are one
            # statement. The identity is therefore tagged `recovered=True` and
            # the orchestrator refuses to act on it for any postprocessor that
            # has not declared its replay safety. See
            # `PostprocessorRecoverySafety`.
            if item.outcome in {Stage3Outcome.LOADED, Stage3Outcome.RECOVERED_RECONCILED}
            and not item.dry_run
            and item.persisted_status == "OK"
        ]

    @property
    def retryable_work_remains(self) -> bool:
        return any(item.retryable for item in self.items)

    @property
    def operator_action_required(self) -> bool:
        return any(item.operator_action_required for item in self.items)

    @property
    def successful_zero_work(self) -> bool:
        return self.attempted_count == 0 and not self.has_failures

    @property
    def has_failures(self) -> bool:
        return any(
            item.outcome.value.startswith("FAILED_")
            or item.outcome in {
                Stage3Outcome.REJECTED_VALIDATION,
                Stage3Outcome.BLOCKED_OPERATOR_ACTION,
                # P0-E. Same reasoning as BLOCKED_OPERATOR_ACTION: nothing
                # technical failed, but the cycle must not report plain success
                # while a file sits in a state only a human can resolve.
                Stage3Outcome.BLOCKED_RECOVERY_OPERATOR,
            }
            for item in self.items
        )

    def to_dict(self) -> dict[str, Any]:
        mapping = {
            "loaded_count": Stage3Outcome.LOADED,
            "recovered_reconciled_count": Stage3Outcome.RECOVERED_RECONCILED,
            "dry_run_success_count": Stage3Outcome.DRY_RUN_SUCCEEDED,
            "skipped_completed_count": Stage3Outcome.SKIPPED_COMPLETED,
            "skipped_ineligible_count": Stage3Outcome.SKIPPED_INELIGIBLE,
            "recovery_deferred_count": Stage3Outcome.SKIPPED_RECOVERY_DEFERRED,
            "blocked_recovery_operator_count": Stage3Outcome.BLOCKED_RECOVERY_OPERATOR,
            "routing_skip_count": Stage3Outcome.SKIPPED_ROUTING,
            "limit_skip_count": Stage3Outcome.SKIPPED_LIMIT,
            "validation_rejected_count": Stage3Outcome.REJECTED_VALIDATION,
            "environment_identity_failure_count": Stage3Outcome.FAILED_ENVIRONMENT_IDENTITY,
            "permission_failure_count": Stage3Outcome.FAILED_PERMISSION,
            "schema_readiness_failure_count": Stage3Outcome.FAILED_SCHEMA_NOT_READY,
            "database_load_failure_count": Stage3Outcome.FAILED_DATABASE_LOAD,
            "writer_validation_failure_count": Stage3Outcome.FAILED_WRITER_VALIDATION,
            "blocked_operator_action_count": Stage3Outcome.BLOCKED_OPERATOR_ACTION,
        }
        retryable = sum(item.retryable for item in self.items)
        non_retryable = sum(
            (
                item.outcome.value.startswith("FAILED_")
                # P0-G. BLOCKED_OPERATOR_ACTION is a terminal configuration
                # block rather than a technical failure, but it is just as
                # non-retryable: nothing changes until a human configures the
                # policy. `_invoke_stage` reads this count to pick the
                # orchestrator's terminal outcome, so leaving it out would let a
                # blocked file end the cycle as SUCCEEDED_WITH_REVIEW_ITEMS —
                # strictly weaker than the FAILED_NON_RETRYABLE the identical
                # condition already produces today from the postprocessor path.
                or item.outcome == Stage3Outcome.BLOCKED_OPERATOR_ACTION
                # P0-E. Identical reasoning for a file whose recovery only a
                # human can decide: it must not be counted as a retryable
                # failure that a later cycle would clear by itself.
                or item.outcome == Stage3Outcome.BLOCKED_RECOVERY_OPERATOR
            )
            and not item.retryable
            for item in self.items
        )
        out = {
            "stage_name": self.stage_name,
            "discovered_candidate_count": self.discovered_candidate_count,
            "eligible_count": self.eligible_count,
            "attempted_count": self.attempted_count,
            "retryable_failure_count": retryable,
            "non_retryable_failure_count": non_retryable,
            "total_inserted_rows": sum(item.inserted_rows for item in self.items),
            "total_updated_rows": sum(item.updated_rows for item in self.items),
            "total_skipped_rows": sum(item.skipped_rows for item in self.items),
            "total_rejected_rows": sum(item.rejected_rows for item in self.items),
            "retryable_work_remains": self.retryable_work_remains,
            "operator_action_required": self.operator_action_required,
            "successful_zero_work": self.successful_zero_work,
            "successful_load_identities": [item.to_dict() for item in self.successful_load_identities],
            "items": [item.to_dict() for item in self.items],
        }
        out.update({name: self.count(outcome) for name, outcome in mapping.items()})
        return out


class Stage3BatchError(RuntimeError):
    def __init__(self, result: Stage3BatchResult):
        self.result = result
        counts = result.to_dict()
        failed_files = sum(
            item.outcome.value.startswith("FAILED_")
            or item.outcome in {Stage3Outcome.REJECTED_VALIDATION, Stage3Outcome.BLOCKED_OPERATOR_ACTION}
            for item in result.items
        )
        super().__init__(
            f"Workflow B Stage 3 failed for {failed_files} raw file(s); "
            "typed batch failures "
            f"(retryable={counts['retryable_failure_count']}, "
            f"non_retryable={counts['non_retryable_failure_count']})"
        )

    @property
    def partial_result(self) -> Stage3BatchResult:
        return self.result
