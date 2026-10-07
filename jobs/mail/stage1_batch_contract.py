from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from jobs.mail.stage1_artifact_sync import (
    Stage1ArtifactReconciliationResult,
    Stage1ArtifactSyncOutcome,
)


class Stage1Outcome(StrEnum):
    CREATED = "CREATED"
    REUSED_MESSAGE = "REUSED_MESSAGE"
    REUSED_RAW_FILE = "REUSED_RAW_FILE"
    REUSED_ARTIFACTS = "REUSED_ARTIFACTS"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_NO_SUPPORTED_ATTACHMENT = "SKIPPED_NO_SUPPORTED_ATTACHMENT"
    SKIPPED_UNSUPPORTED_ATTACHMENT = "SKIPPED_UNSUPPORTED_ATTACHMENT"
    SKIPPED_EXPECTED_LINK = "SKIPPED_EXPECTED_LINK"
    FAILED_RETRYABLE_ARTIFACT_SYNC = "FAILED_RETRYABLE_ARTIFACT_SYNC"
    FAILED_RETRYABLE_PERSISTENCE = "FAILED_RETRYABLE_PERSISTENCE"
    FAILED_RETRYABLE_MAIL_FETCH = "FAILED_RETRYABLE_MAIL_FETCH"
    FAILED_RETRYABLE_DOWNLOAD = "FAILED_RETRYABLE_DOWNLOAD"
    FAILED_NON_RETRYABLE_VALIDATION = "FAILED_NON_RETRYABLE_VALIDATION"
    FAILED_NON_RETRYABLE_CONFIGURATION = "FAILED_NON_RETRYABLE_CONFIGURATION"
    BLOCKED_OPERATOR_ACTION = "BLOCKED_OPERATOR_ACTION"


@dataclass(slots=True)
class Stage1ItemResult:
    outcome: Stage1Outcome
    message_identity: str | None = None
    attachment_identity: str | None = None
    raw_file_id: str | None = None
    raw_artifact_id: str | None = None
    normalized_artifact_id: str | None = None
    deduplication: str = "none"
    artifact_sync_outcomes: list[str] = field(default_factory=list)
    retryable: bool = False
    operator_action_required: bool = False
    error_category: str | None = None
    imap_uid: int | None = None
    client_code: str | None = None
    report_type: str | None = None
    download_host: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None
    expected_bytes: int | None = None
    received_bytes: int | None = None
    retry_attempt: int | None = None
    transaction_scope: str | None = None
    cleanup_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


@dataclass(slots=True)
class Stage1BatchResult:
    stage_name: str = "workflow_b.stage1"
    mailbox_check_completed: bool = False
    messages_inspected: int = 0
    messages_matched: int = 0
    messages_skipped: int = 0
    messages_deduplicated: int = 0
    attachments_discovered: int = 0
    attachments_attempted: int = 0
    attachments_created: int = 0
    attachments_reused: int = 0
    attachments_skipped: int = 0
    unsupported_attachments: int = 0
    raw_files_created: int = 0
    raw_files_reused: int = 0
    reconciliation: Stage1ArtifactReconciliationResult = field(
        default_factory=Stage1ArtifactReconciliationResult
    )
    items: list[Stage1ItemResult] = field(default_factory=list)

    def count(self, outcome: Stage1Outcome) -> int:
        return sum(item.outcome == outcome for item in self.items)

    @property
    def raw_artifacts_created(self) -> int:
        return self._artifact_count("raw", Stage1ArtifactSyncOutcome.CREATED)

    @property
    def raw_artifacts_reused(self) -> int:
        return self._artifact_count("raw", Stage1ArtifactSyncOutcome.REUSED)

    @property
    def normalized_artifacts_created(self) -> int:
        return self._artifact_count("normalized", Stage1ArtifactSyncOutcome.CREATED)

    @property
    def normalized_artifacts_reused(self) -> int:
        return self._artifact_count("normalized", Stage1ArtifactSyncOutcome.REUSED)

    @property
    def artifact_roles_already_synchronized(self) -> int:
        return sum(
            item.outcome
            in {
                Stage1ArtifactSyncOutcome.LINKED_EXISTING,
                Stage1ArtifactSyncOutcome.SKIPPED_ALREADY_SYNCHRONIZED,
            }
            for item in self.reconciliation.items
        )

    def _artifact_count(self, role: str, outcome: Stage1ArtifactSyncOutcome) -> int:
        return sum(
            item.artifact_role == role and item.outcome == outcome
            for item in self.reconciliation.items
        )

    @property
    def retryable_failure_count(self) -> int:
        return sum(
            item.retryable and item.outcome != Stage1Outcome.FAILED_RETRYABLE_ARTIFACT_SYNC
            for item in self.items
        ) + sum(
            item.retryable for item in self.reconciliation.items
        )

    @property
    def non_retryable_failure_count(self) -> int:
        return sum(
            item.outcome
            in {
                Stage1Outcome.FAILED_NON_RETRYABLE_VALIDATION,
                Stage1Outcome.FAILED_NON_RETRYABLE_CONFIGURATION,
            }
            for item in self.items
        ) + sum(
            item.outcome == Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT
            for item in self.reconciliation.items
        )

    @property
    def operator_action_count(self) -> int:
        return sum(
            item.operator_action_required and item.outcome != Stage1Outcome.BLOCKED_OPERATOR_ACTION
            for item in self.items
        ) + sum(
            item.operator_action_required for item in self.reconciliation.items
        )

    @property
    def downstream_stage2_work_may_exist(self) -> bool:
        normalized_repaired = any(
            item.artifact_role == "normalized"
            and item.outcome
            in {
                Stage1ArtifactSyncOutcome.CREATED,
                Stage1ArtifactSyncOutcome.REUSED,
                Stage1ArtifactSyncOutcome.LINKED_EXISTING,
            }
            for item in self.reconciliation.items
        )
        return (self.raw_files_created > 0 and self.attachments_created > 0) or normalized_repaired

    @property
    def retryable_work_remains(self) -> bool:
        return self.retryable_failure_count > 0

    @property
    def operator_action_required(self) -> bool:
        return self.operator_action_count > 0

    @property
    def successful_no_work(self) -> bool:
        return (
            self.mailbox_check_completed
            and not self.downstream_stage2_work_may_exist
            and not self.retryable_work_remains
            and self.non_retryable_failure_count == 0
            and not self.operator_action_required
        )

    @property
    def has_failures(self) -> bool:
        return (
            self.retryable_failure_count > 0
            or self.non_retryable_failure_count > 0
            or self.operator_action_count > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "mailbox_check_completed": self.mailbox_check_completed,
            "messages_inspected": self.messages_inspected,
            "messages_matched": self.messages_matched,
            "messages_skipped": self.messages_skipped,
            "messages_deduplicated": self.messages_deduplicated,
            "attachments_discovered": self.attachments_discovered,
            "attachments_attempted": self.attachments_attempted,
            "attachments_created": self.attachments_created,
            "attachments_reused": self.attachments_reused,
            "attachments_skipped": self.attachments_skipped,
            "unsupported_attachments": self.unsupported_attachments,
            "raw_files_created": self.raw_files_created,
            "raw_files_reused": self.raw_files_reused,
            "raw_artifacts_created": self.raw_artifacts_created,
            "raw_artifacts_reused": self.raw_artifacts_reused,
            "normalized_artifacts_created": self.normalized_artifacts_created,
            "normalized_artifacts_reused": self.normalized_artifacts_reused,
            "artifact_roles_already_synchronized": self.artifact_roles_already_synchronized,
            "reconciliation_records_inspected": self.reconciliation.records_inspected,
            "reconciliation_roles_created": self.reconciliation.count(Stage1ArtifactSyncOutcome.CREATED),
            "reconciliation_roles_reused": self.reconciliation.count(Stage1ArtifactSyncOutcome.REUSED),
            "reconciliation_roles_linked": self.reconciliation.count(Stage1ArtifactSyncOutcome.LINKED_EXISTING),
            "retryable_failure_count": self.retryable_failure_count,
            "non_retryable_failure_count": self.non_retryable_failure_count,
            "operator_action_count": self.operator_action_count,
            "downstream_stage2_work_may_exist": self.downstream_stage2_work_may_exist,
            "retryable_work_remains": self.retryable_work_remains,
            "operator_action_required": self.operator_action_required,
            "successful_no_work": self.successful_no_work,
            "items": [item.to_dict() for item in self.items],
            "artifact_reconciliation": self.reconciliation.to_dict(),
        }


class Stage1BatchError(RuntimeError):
    def __init__(self, result: Stage1BatchResult):
        self.result = result
        categories = ",".join(
            sorted({item.error_category for item in result.items if item.error_category})
        )
        super().__init__(
            "Stage 1 batch contained failures "
            f"(retryable={result.retryable_failure_count}, "
            f"non_retryable={result.non_retryable_failure_count}, "
            f"operator_action={result.operator_action_count}, "
            f"categories={categories or 'none'})"
        )

    @property
    def partial_result(self) -> Stage1BatchResult:
        return self.result
