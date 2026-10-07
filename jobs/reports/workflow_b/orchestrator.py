from __future__ import annotations

import hashlib
import os
import traceback
from datetime import timedelta
from pathlib import Path
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from api.timezone_utils import set_pg_session_timezone
from jobs.mail.fetch_reports import fetch_reports_batch
from jobs.mail.stage1_batch_contract import Stage1BatchError, Stage1BatchResult
from jobs.mail.stage1_batch_contract import Stage1Outcome
from jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment import (
    AMBIGUOUS_ENRICHMENT_MATCH,
    COVERAGE_BELOW_THRESHOLD,
    EnrichmentPreconditionError,
)
from jobs.reports.stage2.batch_contract import Stage2BatchError, Stage2BatchResult, Stage2Outcome
from jobs.reports.stage2.job_stage2 import process_stage2_batch
from jobs.reports.stage3.batch_contract import (
    Stage3BatchError,
    Stage3BatchResult,
    Stage3SuccessfulLoadIdentity,
)
from jobs.reports.stage3.job_stage3 import process_stage3_batch
from jobs.reports.stage3.schema_readiness import (
    RuntimeSchemaMutationDisabledError,
    Stage3SchemaReadinessError,
)
from jobs.reports.workflow_b.trip_metrics_selector import (
    SelectorCompatibility,
    WorkflowBSelectorResolutionError,
    load_report_policy_selector_state,
    resolve_workflow_b_trip_metrics_population_source,
)
from jobs.reports.workflow_b.unresolved_inputs import (
    UnresolvedInputSignalResult,
    one_shot_unresolved_input_count,
    signal_unresolved_inputs,
)
from jobs.reports.workflow_b.postprocessor_registry import (
    applicable_postprocessors,
    ALPHA_DYSPONENT_POSTPROCESSOR,
    ALPHA_CLIENT_CODE,
    ALPHA_SOURCE_REPORT,
    PostprocessorExecutionMode,
    REPORT_207_POSTPROCESSOR,
    UnsupportedPostprocessorConfiguration,
    dependency_postprocessors,
    resolve_postprocessor,
)


JOB_SOURCE = "jobs.reports.workflow_b.orchestrator"
WORKFLOW_B_LOCK_NAMESPACE = "workflow_b.orchestrator.v1"
ALPHA_SOURCE_REPORT_KEY = "gps_baza_start_skrypt"

# Used only to shorten traceback filenames; never to resolve anything.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class WorkflowBOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_NO_WORK = "SUCCEEDED_NO_WORK"
    SUCCEEDED_WITH_REVIEW_ITEMS = "SUCCEEDED_WITH_REVIEW_ITEMS"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NON_RETRYABLE = "FAILED_NON_RETRYABLE"
    BLOCKED_OPERATOR_ACTION = "BLOCKED_OPERATOR_ACTION"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    # A *scheduled* cycle that never executed because another execution owned the
    # orchestration lock. Deliberately distinct from SKIPPED_LOCKED, which is a
    # manual_diagnostic run correctly yielding to the scheduled owner — benign,
    # and it must stay silent. This one means a required cycle did not happen at
    # all, and Workflow B fires only at 06:00 and 20:00, so there is no near-term
    # natural retry that would absorb it. Reporting it as a completed cycle is
    # what let lock contention silently satisfy autonomous coverage.
    BLOCKED_CONCURRENT_EXECUTION = "BLOCKED_CONCURRENT_EXECUTION"
    FAILED_UNEXPECTED_STAGE_ERROR = "FAILED_UNEXPECTED_STAGE_ERROR"


class WorkflowBPostprocessorOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    SKIPPED_NOT_CONFIGURED = "SKIPPED_NOT_CONFIGURED"
    SKIPPED_ALREADY_COMPLETED = "SKIPPED_ALREADY_COMPLETED"
    SKIPPED_DUPLICATE_PLAN = "SKIPPED_DUPLICATE_PLAN"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NON_RETRYABLE = "FAILED_NON_RETRYABLE"
    FAILED_ENVIRONMENT_IDENTITY = "FAILED_ENVIRONMENT_IDENTITY"
    FAILED_PERMISSION = "FAILED_PERMISSION"
    FAILED_SCHEMA_NOT_READY = "FAILED_SCHEMA_NOT_READY"
    BLOCKED_UNSUPPORTED_CONFIGURATION = "BLOCKED_UNSUPPORTED_CONFIGURATION"
    BLOCKED_OPERATOR_ACTION = "BLOCKED_OPERATOR_ACTION"


class AlphaSourceRefreshOutcome(StrEnum):
    NO_NEWER_SOURCE_AVAILABLE = "NO_NEWER_SOURCE_AVAILABLE"
    ALREADY_LOADED = "ALREADY_LOADED"
    SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_PASSED = "SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_PASSED"
    SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_FAILED = "SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_FAILED"
    BLOCKED_TARGET_WRITE_DETECTED = "BLOCKED_TARGET_WRITE_DETECTED"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"


@dataclass(slots=True)
class WorkflowBStageExecution:
    stage_name: str
    started: bool = False
    completed: bool = False
    typed_partial_failure: bool = False
    unexpected_failure: bool = False
    retryable_failure: bool = False
    non_retryable_failure: bool = False
    operator_action_required: bool = False
    error_category: str | None = None
    # Preserved for an unexpected stage exception. `_invoke_stage` used to catch
    # bare `Exception` without binding it, so the type, the message and the
    # traceback of the only failure class that carries no typed partial result
    # were all discarded — the operator was left with the string
    # "unexpected_stage_error" and nothing else.
    exception_type: str | None = None
    exception_message: str | None = None
    stack_trace: str | None = field(default=None, repr=False)
    # Bounded structured origin: the innermost frames of the stage exception as
    # "path:line:function", nothing else. `stack_trace` above lives only in
    # memory and dies with the process; these frames are serialized, so the
    # originating line is still identifiable from `public.logs` and from the
    # incident after the process exits and journald has rotated.
    #
    # Frames only — never source text, never local variables, never the
    # environment. Python's own `format_exc()` does not include locals either,
    # and nothing here reintroduces them.
    origin_frames: tuple[str, ...] = ()
    # The live exception, kept solely so `_finish_or_raise` can chain it as
    # `__cause__`. Not serialized: `exception_type`, `exception_message` and
    # `origin_frames` are the durable projection of it.
    exception: BaseException | None = field(default=None, repr=False, compare=False)
    result: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        summary = _safe_stage_summary(self.result)
        return {
            "stage_name": self.stage_name,
            "started": self.started,
            "completed": self.completed,
            "typed_partial_failure": self.typed_partial_failure,
            "unexpected_failure": self.unexpected_failure,
            "retryable_failure": self.retryable_failure,
            "non_retryable_failure": self.non_retryable_failure,
            "operator_action_required": self.operator_action_required,
            "error_category": self.error_category,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
            "origin_frames": list(self.origin_frames),
            "result_summary": summary,
        }

    @property
    def failed(self) -> bool:
        return bool(
            self.unexpected_failure
            or self.retryable_failure
            or self.non_retryable_failure
            or self.typed_partial_failure
        )


@dataclass(frozen=True, slots=True)
class WorkflowBPostprocessorPlan:
    postprocessor_name: str
    raw_file_id: str
    client_code: str
    report_type: str
    source_cleaned_artifact_id: str | None
    selector: str
    selector_origin: str = "client_default"
    error_category: str | None = None
    destination_schema: str | None = None
    destination_table: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict, compare=False)
    execution_mode: PostprocessorExecutionMode = PostprocessorExecutionMode.EXECUTE

    @property
    def stable_identity(self) -> tuple[str, str, str, str | None]:
        return (
            self.postprocessor_name,
            self.raw_file_id,
            self.client_code,
            self.source_cleaned_artifact_id,
        )


@dataclass(slots=True)
class WorkflowBPostprocessorResult:
    postprocessor_name: str
    raw_file_id: str
    client_code: str
    report_type: str
    outcome: WorkflowBPostprocessorOutcome
    retryable: bool = False
    operator_action_required: bool = False
    error_category: str | None = None
    error_detail: str | None = None
    candidate_rows: int = 0
    migrated_rows: int = 0
    records_affected: int = 0
    execution_mode: PostprocessorExecutionMode = PostprocessorExecutionMode.EXECUTE
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        value["execution_mode"] = self.execution_mode.value
        return value


@dataclass(slots=True)
class WorkflowBBatchResult:
    workflow_name: str = "workflow_b"
    outcome: WorkflowBOutcome = WorkflowBOutcome.SUCCEEDED_NO_WORK
    lock_acquired: bool = False
    # "scheduled" or "manual_diagnostic". Recorded because it is what decides
    # whether losing the orchestration lock is a missed required cycle or a
    # manual run correctly standing aside.
    mode: str = "scheduled"
    stage1: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage1"))
    stage2: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage2"))
    stage3: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage3"))
    postprocessors: list[WorkflowBPostprocessorResult] = field(default_factory=list)
    postprocessor_plans_discovered: int = 0
    duplicate_postprocessor_plans: int = 0
    # What the per-input signalling pass did this cycle. Present on every path
    # that entered the stages; None means the pass did not run at all — a lock
    # loser, or a signalling failure that was deliberately not allowed to
    # replace the cycle's own outcome.
    unresolved_input_signal: UnresolvedInputSignalResult | None = None
    # The one signalling condition that may change this cycle's verdict: a
    # material one-shot unresolved input was observed and the pass could
    # establish neither an individual nor an aggregate durable incident for it.
    # Its evidence is gone with this cycle, so a healthy-looking terminal state
    # would be a false statement about durable coverage. Nothing else about
    # signalling reaches this field — see `_finish_cycle`.
    unresolved_signal_safety_failure: bool = False

    @property
    def stages(self) -> tuple[WorkflowBStageExecution, ...]:
        return (self.stage1, self.stage2, self.stage3)

    @property
    def stages_started_count(self) -> int:
        return sum(stage.started for stage in self.stages)

    @property
    def stages_completed_count(self) -> int:
        return sum(stage.completed for stage in self.stages)

    @property
    def stages_with_typed_partial_failure_count(self) -> int:
        return sum(stage.typed_partial_failure for stage in self.stages)

    @property
    def retryable_failure_count(self) -> int:
        return sum(stage.retryable_failure for stage in self.stages) + sum(item.retryable for item in self.postprocessors)

    @property
    def non_retryable_failure_count(self) -> int:
        return sum(stage.non_retryable_failure or stage.unexpected_failure for stage in self.stages) + sum(
            item.outcome in {
                WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE,
                WorkflowBPostprocessorOutcome.FAILED_ENVIRONMENT_IDENTITY,
                WorkflowBPostprocessorOutcome.FAILED_PERMISSION,
                WorkflowBPostprocessorOutcome.FAILED_SCHEMA_NOT_READY,
                WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
                WorkflowBPostprocessorOutcome.BLOCKED_OPERATOR_ACTION,
            }
            for item in self.postprocessors
        )

    @property
    def review_operator_action_count(self) -> int:
        return sum(stage.operator_action_required for stage in self.stages) + sum(
            item.operator_action_required for item in self.postprocessors
        )

    @property
    def useful_work_occurred(self) -> bool:
        stage1 = self.stage1.result
        stage2 = self.stage2.result
        stage3 = self.stage3.result
        return bool(
            (isinstance(stage1, Stage1BatchResult) and (
                stage1.raw_files_created or stage1.raw_artifacts_created or stage1.raw_artifacts_reused
                or stage1.normalized_artifacts_created or stage1.normalized_artifacts_reused
            ))
            or (isinstance(stage2, Stage2BatchResult) and (
                stage2.count(Stage2Outcome.SUCCEEDED_CREATED)
                or stage2.count(Stage2Outcome.SUCCEEDED_REUSED)
            ))
            or (isinstance(stage3, Stage3BatchResult) and bool(stage3.successful_load_identities))
            or any(item.outcome == WorkflowBPostprocessorOutcome.SUCCEEDED and item.records_affected > 0 for item in self.postprocessors)
        )

    @property
    def retryable_work_remains(self) -> bool:
        return self.retryable_failure_count > 0

    @property
    def operator_action_required(self) -> bool:
        return self.review_operator_action_count > 0

    @property
    def successful_zero_work(self) -> bool:
        return self.outcome == WorkflowBOutcome.SUCCEEDED_NO_WORK

    @property
    def cycle_executed(self) -> bool:
        """Did this invocation actually run the Workflow B cycle?

        The distinction P0-D exists to make. `SUCCEEDED_NO_WORK` means a real
        cycle ran, looked, and found nothing to do — that satisfies the schedule.
        A lock-contention outcome means the stages were never entered at all, so
        nothing was looked at and nothing can be concluded about outstanding work.
        Collapsing the two is what let a skipped cycle report operational success.

        Derived from `lock_acquired` rather than stored separately: the lock is
        the only gate between "returned early" and "ran", and a second field
        could drift away from it.
        """
        return self.lock_acquired

    @property
    def postprocessor_plans_executed(self) -> int:
        return len(self.postprocessors)

    @property
    def postprocessor_plans_skipped(self) -> int:
        return self.duplicate_postprocessor_plans + sum(item.outcome.value.startswith("SKIPPED_") for item in self.postprocessors)

    @property
    def postprocessor_successes(self) -> int:
        return sum(item.outcome == WorkflowBPostprocessorOutcome.SUCCEEDED for item in self.postprocessors)

    @property
    def failing_stage(self) -> WorkflowBStageExecution | None:
        """The earliest stage that failed, which is the one that caused the rest."""
        for stage in self.stages:
            if stage.failed:
                return stage
        return None

    @property
    def failed_postprocessors(self) -> list[WorkflowBPostprocessorResult]:
        return [
            item for item in self.postprocessors
            if item.outcome.value.startswith(("FAILED_", "BLOCKED_"))
        ]

    @property
    def production_loads_succeeded(self) -> int:
        return (
            len(self.stage3.result.successful_load_identities)
            if isinstance(self.stage3.result, Stage3BatchResult) else 0
        )

    def incident_details(self) -> dict[str, Any]:
        """Sanitized, incident-shaped evidence for the operator alert.

        Deliberately small and flat: this is what an operator needs in an email
        to decide whether to act now, not the full result payload — that already
        goes to `public.logs`. Every field is reported only when genuinely known;
        nothing is defaulted or invented, because a fabricated client code is
        worse than an absent one.

        `durable_writes_committed` is the field that changes the operator's first
        move: a Stage 3 commit followed by a postprocessor failure means customer
        data already changed, and a blind re-run is not obviously safe.
        """
        details: dict[str, Any] = {
            "workflow_name": self.workflow_name,
            "terminal_outcome": self.outcome.value,
            "lock_acquired": self.lock_acquired,
            "cycle_executed": self.cycle_executed,
            "mode": self.mode,
            "stages_completed_count": self.stages_completed_count,
            "production_loads_succeeded": self.production_loads_succeeded,
            "durable_writes_committed": self.production_loads_succeeded > 0,
            "retryable_failure_count": self.retryable_failure_count,
            "non_retryable_failure_count": self.non_retryable_failure_count,
        }
        stage = self.failing_stage
        if stage is not None:
            details["failing_stage"] = stage.stage_name
            details["stage_error_category"] = stage.error_category
            if stage.exception_type:
                details["stage_exception_type"] = stage.exception_type
            if stage.exception_message:
                details["stage_exception_message"] = stage.exception_message
            if stage.origin_frames:
                # Bounded `path:line:function` frames, no source and no locals.
                # Recorded under `details`, which `SuspectedBugEvent` excludes
                # from `fingerprint_identity()` — so a line number moving between
                # releases cannot split one recurring incident into two.
                details["stage_origin_frames"] = list(stage.origin_frames)
                details["stage_origin"] = stage.origin_frames[-1]
        failed = self.failed_postprocessors
        if failed:
            first = failed[0]
            details["failing_postprocessor"] = first.postprocessor_name
            details["postprocessor_outcome"] = first.outcome.value
            details["client_code"] = first.client_code
            details["report_type"] = first.report_type
            details["raw_file_id"] = first.raw_file_id
            if first.error_category:
                details["postprocessor_error_category"] = first.error_category
            if len(failed) > 1:
                details["failing_postprocessor_count"] = len(failed)
        if self.unresolved_signal_safety_failure:
            # The operator's first move is different from every other Workflow B
            # failure: nothing is wrong with the pipeline, but an input that
            # arrived has no durable record anywhere and the cycle's logs are
            # the only place its identity still exists.
            details["unresolved_signal_safety_failure"] = True
            signal = self.unresolved_input_signal
            if signal is not None:
                details["one_shot_without_durable_evidence"] = (
                    signal.one_shot_without_durable_evidence
                )
                details["one_shot_input_count"] = signal.one_shot_input_count
        if self.outcome == WorkflowBOutcome.BLOCKED_CONCURRENT_EXECUTION:
            # No stage ran, so `failing_stage` is None and the operator would
            # otherwise get an incident with no cause at all. Name the lock and
            # say plainly that nothing was written, which is what decides whether
            # re-running by hand is safe.
            details["lock_namespace"] = WORKFLOW_B_LOCK_NAMESPACE
            details["blocked_reason"] = "another_execution_owns_the_orchestration_lock"
        return {key: value for key, value in details.items() if value is not None}

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "outcome": self.outcome.value,
            "lock_acquired": self.lock_acquired,
            "cycle_executed": self.cycle_executed,
            "mode": self.mode,
            "stage1": self.stage1.to_dict(),
            "stage2": self.stage2.to_dict(),
            "stage3": self.stage3.to_dict(),
            "postprocessors": [item.to_dict() for item in self.postprocessors],
            "stages_started_count": self.stages_started_count,
            "stages_completed_count": self.stages_completed_count,
            "stages_with_typed_partial_failure_count": self.stages_with_typed_partial_failure_count,
            "postprocessor_plans_discovered": self.postprocessor_plans_discovered,
            "postprocessor_plans_executed": self.postprocessor_plans_executed,
            "postprocessor_plans_skipped": self.postprocessor_plans_skipped,
            "postprocessor_successes": self.postprocessor_successes,
            "retryable_failure_count": self.retryable_failure_count,
            "non_retryable_failure_count": self.non_retryable_failure_count,
            "review_operator_action_count": self.review_operator_action_count,
            "useful_work_occurred": self.useful_work_occurred,
            "production_loads_succeeded": self.production_loads_succeeded,
            "retryable_work_remains": self.retryable_work_remains,
            "operator_action_required": self.operator_action_required,
            "successful_zero_work": self.successful_zero_work,
            "unresolved_input_signal": (
                self.unresolved_input_signal.to_dict()
                if self.unresolved_input_signal is not None else None
            ),
            "unresolved_signal_safety_failure": self.unresolved_signal_safety_failure,
        }


@dataclass(frozen=True, slots=True)
class AlphaSourceSelection:
    identity: Stage3SuccessfulLoadIdentity
    requires_stage3: bool
    newer_source_available: bool


@dataclass(slots=True)
class AlphaSourceRefreshResult:
    workflow_b_run_id: str | None = None
    outcome: AlphaSourceRefreshOutcome = AlphaSourceRefreshOutcome.NO_NEWER_SOURCE_AVAILABLE
    lock_acquired: bool = False
    source_load_committed: bool = False
    normal_execute_postprocessor_outstanding: bool = False
    stage1: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage1"))
    stage2: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage2"))
    stage3: WorkflowBStageExecution = field(default_factory=lambda: WorkflowBStageExecution("workflow_b.stage3"))
    successful_load_identity: Stage3SuccessfulLoadIdentity | None = None
    postprocessor: WorkflowBPostprocessorResult | None = None
    target_rows_modified: int = 0

    @property
    def postprocessor_dry_run_passed(self) -> bool:
        return bool(
            self.postprocessor
            and self.postprocessor.execution_mode is PostprocessorExecutionMode.DRY_RUN
            and self.postprocessor.outcome in {
                WorkflowBPostprocessorOutcome.SUCCEEDED,
                WorkflowBPostprocessorOutcome.SKIPPED_ALREADY_COMPLETED,
            }
            and self.target_rows_modified == 0
            and self.postprocessor.details.get("readiness_passed") is not False
        )

    def to_dict(self) -> dict[str, Any]:
        details = dict(self.postprocessor.details) if self.postprocessor else {}
        return {
            "operation": "alpha00001_source_refresh_for_backfill",
            "workflow_b_run_id": self.workflow_b_run_id,
            "outcome": self.outcome.value,
            "lock_acquired": self.lock_acquired,
            "source_load_committed": self.source_load_committed,
            "normal_execute_postprocessor_outstanding": self.normal_execute_postprocessor_outstanding,
            "stage1": self.stage1.to_dict(),
            "stage2": self.stage2.to_dict(),
            "stage3": self.stage3.to_dict(),
            "successful_load_identity": (
                self.successful_load_identity.to_dict()
                if self.successful_load_identity else None
            ),
            "postprocessor": self.postprocessor.to_dict() if self.postprocessor else None,
            "postprocessor_dry_run_passed": self.postprocessor_dry_run_passed,
            "target_rows_modified": self.target_rows_modified,
            "source_destination": "telematics_reports.Alpha_GPS_Baza_LOG",
            "source_raw_file_id": details.get("source_raw_file_id") or (
                self.successful_load_identity.raw_file_id
                if self.successful_load_identity else None
            ),
            "source_workflow_run_id": details.get("source_workflow_run_id"),
            "source_cleaned_artifact_id": details.get("source_cleaned_artifact_id") or (
                self.successful_load_identity.source_cleaned_artifact_id
                if self.successful_load_identity else None
            ),
            "source_row_count": details.get("source_rows_inspected"),
            "source_load_timestamp": details.get("source_loaded_at"),
            "maximum_source_assignment_date": details.get("source_business_date_max"),
            "source_age_hours": details.get("source_age_hours"),
            "source_target_overlap": {
                "start": details.get("resolved_start_date"),
                "end_exclusive": details.get("resolved_end_date_exclusive"),
            } if details else None,
            "target_trips_inspected": details.get("target_trips_in_scope"),
            "planned_updates": details.get("planned_updates"),
            "ambiguities": details.get("ambiguous_source_matches"),
            "conflicts": details.get("conflicting_existing_target_values"),
            "predicted_trip_coverage": details.get("predicted_trip_coverage_percent"),
            "predicted_distance_coverage": details.get("predicted_distance_coverage_percent"),
            "readiness_result": details.get("status"),
        }


class Stage3RecoveryReofferNotDeclaredSafe(RuntimeError):
    """P0-E. A reconciled load would re-offer an undeclared postprocessor.

    Fail-closed on purpose. A `recovered=True` identity means the destination
    load committed but the process died afterwards, possibly before the
    postprocessor ran. Re-offering it is how that gap is closed — but only for a
    postprocessor whose replay safety has been established against its actual
    writer (`PostprocessorRecoverySafety`). For anything else, applying a
    possibly-irreversible effect a second time is worse than stopping, so the
    cycle fails visibly and an operator decides.

    Not a silent skip: silently dropping the plan would leave the file looking
    fully processed while its postprocessing never happened.
    """

    def __init__(self, identity, unsafe_names: list[str]):
        self.identity = identity
        self.unsafe_names = list(unsafe_names)
        super().__init__(
            "Stage 3 recovery reconciled a committed load, but postprocessor(s) "
            f"{', '.join(self.unsafe_names)} have not declared recovery replay safety "
            f"(raw_file_id={identity.raw_file_id}, client_code={identity.client_code}, "
            f"report_type={identity.report_type})"
        )


class WorkflowBOrchestrationError(RuntimeError):
    def __init__(self, result: WorkflowBBatchResult):
        self.result = result
        # Consumed by ops.operational_alert.report_job_terminal_failure through
        # its duck-typed INCIDENT_DETAILS_ATTRIBUTE contract. The runner boundary
        # sees only the module name and empty params, so without this the
        # delivered alert cannot say which stage failed, what the terminal
        # outcome was, or whether Stage 3 already committed customer data.
        #
        # Both are guarded: this constructor runs on the failure path, and an
        # exception raised while *describing* a failure would replace it with a
        # completely unrelated one. Degrading to less evidence is always better
        # than losing the failure being reported.
        try:
            self.operational_incident_details = result.incident_details()
        except Exception:
            self.operational_incident_details = {}
        try:
            message = _orchestration_error_message(result)
        except Exception:
            message = "Workflow B orchestration failed"
        super().__init__(message)

    @property
    def partial_result(self) -> WorkflowBBatchResult:
        return self.result


def _orchestration_error_message(result: WorkflowBBatchResult) -> str:
    """Name the cause in the exception text itself.

    `public.logs` records the exception string for a failed run, and
    `error_signature()` fingerprints on it. The bare outcome collapsed a Stage 2
    crash and a postprocessor configuration block onto one signature; naming the
    failing stage and exception type separates them without adding per-attempt
    noise (no ids, no timestamps, no counts).
    """
    parts = [f"Workflow B orchestration failed: {result.outcome.value}"]
    if result.outcome == WorkflowBOutcome.BLOCKED_CONCURRENT_EXECUTION:
        # No stage ran, so the stage clause below would add nothing. Naming the
        # lock keeps this signature distinct from every stage failure without
        # adding anything per-attempt (no ids, no timestamps, no counts), so the
        # anti-storm fingerprint still folds repeats onto one incident.
        parts.append(f"lock={WORKFLOW_B_LOCK_NAMESPACE}")
        return "; ".join(parts)
    stage = result.failing_stage
    if stage is not None:
        detail = stage.exception_type or stage.error_category
        parts.append(f"stage={stage.stage_name}" + (f" ({detail})" if detail else ""))
    elif result.unresolved_signal_safety_failure:
        # No stage failed, so the clause above adds nothing and the bare outcome
        # would collapse this onto every other non-retryable failure. Naming the
        # cause keeps `error_signature()` distinct without anything per-attempt
        # (no ids, no timestamps, no counts).
        parts.append("cause=one_shot_unresolved_input_without_durable_signal")
    return "; ".join(parts)


def workflow_b_advisory_lock_key() -> int:
    digest = hashlib.sha256(WORKFLOW_B_LOCK_NAMESPACE.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _platform_pg_conn():
    import psycopg
    from psycopg.rows import dict_row

    return set_pg_session_timezone(psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "logdb"),
        user=os.getenv("POSTGRES_USER", "loguser"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        row_factory=dict_row,
    ))


def _try_global_lock(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (workflow_b_advisory_lock_key(),))
        row = cur.fetchone()
    conn.commit()
    return bool(row.get("pg_try_advisory_lock") if isinstance(row, dict) else row[0])


def _release_global_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (workflow_b_advisory_lock_key(),))
    conn.commit()


def _relinquish_lock_session(client, run_id: str, conn, *, acquired: bool) -> None:
    """End the lock session without ever replacing the cycle's own outcome.

    `workflow_b.orchestrator.v1` is a **session-level** advisory lock, so the
    thing that reliably relinquishes it is the session ending — closing the
    connection — not the explicit `pg_advisory_unlock`. The explicit unlock is
    still issued because it is the cheap, legible release on the healthy path;
    it just may not be the last word.

    This runs from `finally`, which is the whole problem it solves. An exception
    raised there replaces whatever the block was already returning or raising, so
    a broken connection would silently overwrite either a healthy result or the
    original `WorkflowBOrchestrationError` with a psycopg error from cleanup.
    Both steps are therefore contained, and `close()` is reached even when the
    unlock raises.
    """
    if acquired:
        try:
            _release_global_lock(conn)
        except Exception as exc:
            try:
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Workflow B advisory unlock failed; closing the lock session instead",
                    run_id=run_id,
                    context={
                        "lock_namespace": WORKFLOW_B_LOCK_NAMESPACE,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                    },
                )
            except Exception:
                pass
    try:
        conn.close()
    except Exception:
        # A session that cannot be closed cleanly is still gone from the
        # server's point of view once the socket drops, and there is nothing
        # left in this cycle that a raise here could usefully tell anyone.
        pass


def _validate_params(params: dict) -> tuple[dict, dict, dict]:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    unknown = set(params) - {"mode", "stage1", "stage2", "stage3", "postprocessors", "trigger", "actor"}
    if unknown:
        raise ValueError(f"Unsupported Workflow B orchestrator parameters: {sorted(unknown)}")
    mode = str(params.get("mode") or "scheduled").strip().lower()
    if mode not in {"scheduled", "manual_diagnostic"}:
        raise ValueError("mode must be scheduled or manual_diagnostic")
    stage1 = dict(params.get("stage1") or {})
    stage2 = dict(params.get("stage2") or {})
    stage3 = dict(params.get("stage3") or {})
    postprocessors = dict(params.get("postprocessors") or {})
    if postprocessors:
        raise ValueError("postprocessor selector overrides are not supported")
    unsafe = []
    if stage2.get("force_reprocess"):
        unsafe.append("stage2.force_reprocess")
    if stage2.get("input_files") or stage2.get("input_dir"):
        unsafe.append("stage2 path inputs")
    if stage3.get("force_reprocess"):
        unsafe.append("stage3.force_reprocess")
    if stage3.get("dry_run"):
        unsafe.append("stage3.dry_run")
    if mode == "scheduled" and (stage2.get("raw_file_ids") or stage3.get("raw_file_id")):
        unsafe.append("scheduled explicit raw-file filters")
    if unsafe:
        raise ValueError("Unsafe Workflow B orchestrator parameters: " + ", ".join(unsafe))
    return stage1, stage2, stage3, mode


def run_workflow_b_batch(client, run_id: str, params: dict) -> WorkflowBBatchResult:
    stage1_params, stage2_params, stage3_params, mode = _validate_params(params or {})
    result = WorkflowBBatchResult(mode=mode)
    lock_conn = _platform_pg_conn()
    try:
        if not _try_global_lock(lock_conn):
            # P0-D. Losing the lock means the stages were never entered, so this
            # invocation observed nothing and completed nothing. What that means
            # depends entirely on who lost:
            #
            #   manual_diagnostic — an operator's ad-hoc run standing aside for
            #     the scheduled owner. Exactly the intended ownership model, and
            #     the scheduled run is still going to do the work. Silent.
            #
            #   scheduled — the required cycle did not happen. Workflow B fires
            #     only at 06:00 and 20:00, so nothing retries it for another
            #     10-14 hours; and contention at a scheduled fire means either a
            #     previous cycle is still running many hours later or a manual
            #     run is holding the lock. Both need an operator.
            #
            # The scheduled case raises so `run_context` records the run FAILED.
            # That is the durable fact the watchdog reads: a FAILED run is
            # classified EXPECTED_FAILED and never counted as a satisfied fire,
            # whereas the previous `return result` produced SUCCESS and silently
            # satisfied it. No new schema, no new alert channel — the existing
            # terminal-failure path already carries this.
            result.lock_acquired = False
            if mode != "scheduled":
                result.outcome = WorkflowBOutcome.SKIPPED_LOCKED
                return result
            result.outcome = WorkflowBOutcome.BLOCKED_CONCURRENT_EXECUTION
            raise WorkflowBOrchestrationError(result)
        result.lock_acquired = True
        client.log("INFO", "SCRIPT", JOB_SOURCE, "Workflow B orchestration started", run_id=run_id, context={"lock_namespace": WORKFLOW_B_LOCK_NAMESPACE})

        if not _invoke_stage(result.stage1, fetch_reports_batch, Stage1BatchError, client, run_id, stage1_params):
            return _finish_cycle(client, run_id, result)
        if not _invoke_stage(result.stage2, process_stage2_batch, Stage2BatchError, client, run_id, stage2_params):
            return _finish_cycle(client, run_id, result)
        if not _invoke_stage(result.stage3, process_stage3_batch, Stage3BatchError, client, run_id, stage3_params):
            return _finish_cycle(client, run_id, result)

        identities = result.stage3.result.successful_load_identities if isinstance(result.stage3.result, Stage3BatchResult) else []
        plans, skipped = _discover_postprocessor_plans(lock_conn, identities)
        result.postprocessor_plans_discovered = len(plans) + skipped
        result.duplicate_postprocessor_plans = skipped
        for plan in plans:
            result.postprocessors.append(_execute_postprocessor_plan(client, run_id, plan))
        return _finish_cycle(client, run_id, result)
    finally:
        _relinquish_lock_session(client, run_id, lock_conn, acquired=result.lock_acquired)


def _alpha_identity(row: dict[str, Any]) -> Stage3SuccessfulLoadIdentity:
    return Stage3SuccessfulLoadIdentity(
        raw_file_id=str(row["raw_file_id"]),
        client_code=ALPHA_CLIENT_CODE,
        report_type=ALPHA_SOURCE_REPORT,
        destination_schema="telematics_reports",
        destination_table=ALPHA_SOURCE_REPORT,
        source_cleaned_artifact_id=(
            str(row["source_cleaned_artifact_id"])
            if row.get("source_cleaned_artifact_id") else None
        ),
        final_status=str(row.get("stage3_status") or "PENDING"),
    )


def _select_alpha_source_for_refresh(
    conn, raw_file_ids: list[str]
) -> AlphaSourceSelection | None:
    """Choose only the exact ALPHA report; never route another Stage 2 result."""
    candidate = None
    if raw_file_ids:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rf.id::text AS raw_file_id,
                       rf.stage2_cleaned_artifact_id::text AS source_cleaned_artifact_id,
                       rf.stage3_status,
                       COALESCE(msg.internal_date, msg.fetched_at) AS source_message_at
                FROM ingest.raw_file rf
                JOIN ingest.imap_message msg ON msg.id = rf.imap_message_id
                WHERE rf.id = ANY(%s::uuid[])
                  AND rf.status = 'NORMALIZED'
                  AND rf.stage2_status = 'OK'
                  AND rf.client_code = %s
                  AND rf.stage2_report_type = %s
                ORDER BY COALESCE(msg.internal_date, msg.fetched_at) DESC, rf.id DESC
                LIMIT 1
                """,
                (raw_file_ids, ALPHA_CLIENT_CODE, ALPHA_SOURCE_REPORT),
            )
            candidate = cur.fetchone()
        conn.rollback()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rf.id::text AS raw_file_id,
                   rf.stage2_cleaned_artifact_id::text AS source_cleaned_artifact_id,
                   rf.stage3_status,
                   COALESCE(msg.internal_date, msg.fetched_at) AS source_message_at,
                   (run.status = 'FAILED' AND run.source =
                     'ops.refresh_alpha00001_source_for_backfill') AS retry_dry_run
            FROM ingest.raw_file rf
            JOIN ingest.imap_message msg ON msg.id = rf.imap_message_id
            LEFT JOIN runs run ON run.run_id = msg.run_id
            WHERE rf.status = 'NORMALIZED'
              AND rf.stage2_status = 'OK'
              AND rf.stage3_status = 'OK'
              AND rf.client_code = %s
              AND rf.stage2_report_type = %s
              AND rf.stage3_destination_schema = 'telematics_reports'
              AND rf.stage3_destination_table = %s
            ORDER BY COALESCE(msg.internal_date, msg.fetched_at) DESC,
                     rf.stage3_finished_at DESC, rf.id DESC
            LIMIT 1
            """,
            (ALPHA_CLIENT_CODE, ALPHA_SOURCE_REPORT, ALPHA_SOURCE_REPORT),
        )
        latest = cur.fetchone()
    conn.rollback()

    if candidate is None:
        return (
            AlphaSourceSelection(_alpha_identity(latest), False, False)
            if latest and latest.get("retry_dry_run") else None
        )
    if str(candidate.get("stage3_status") or "") == "OK":
        return AlphaSourceSelection(_alpha_identity(candidate), False, False)
    if latest and candidate.get("source_message_at") and latest.get("source_message_at"):
        if candidate["source_message_at"] <= latest["source_message_at"]:
            return AlphaSourceSelection(_alpha_identity(latest), False, False)
    return AlphaSourceSelection(_alpha_identity(candidate), True, True)


def _alpha_stage1_raw_file_ids(conn, stage1: Stage1BatchResult) -> list[str]:
    accepted = {Stage1Outcome.CREATED, Stage1Outcome.REUSED_RAW_FILE}
    discovered = list(dict.fromkeys(
        str(item.raw_file_id)
        for item in stage1.items
        if item.raw_file_id and item.outcome in accepted
    ))
    if not discovered:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS raw_file_id
            FROM ingest.raw_file
            WHERE id = ANY(%s::uuid[])
              AND status = 'NORMALIZED'
              AND report_key = %s
            ORDER BY id
            """,
            (discovered, ALPHA_SOURCE_REPORT_KEY),
        )
        selected = [str(row["raw_file_id"]) for row in cur.fetchall()]
    conn.rollback()
    return selected


def run_alpha_source_refresh_batch(client, run_id: str) -> AlphaSourceRefreshResult:
    """Commit an exact ALPHA source, then evaluate enrichment in dry-run mode."""
    result = AlphaSourceRefreshResult(workflow_b_run_id=run_id)
    lock_conn = _platform_pg_conn()
    try:
        if not _try_global_lock(lock_conn):
            result.outcome = AlphaSourceRefreshOutcome.SKIPPED_LOCKED
            return result
        result.lock_acquired = True

        if not _invoke_stage(result.stage1, fetch_reports_batch, Stage1BatchError, client, run_id, {}):
            raise RuntimeError("ALPHA source refresh Stage 1 failed unexpectedly")
        raw_file_ids = _alpha_stage1_raw_file_ids(lock_conn, result.stage1.result)
        if raw_file_ids:
            stage2_params = {"raw_file_ids": raw_file_ids, "limit": len(raw_file_ids)}
            if not _invoke_stage(
                result.stage2, process_stage2_batch, Stage2BatchError,
                client, run_id, stage2_params,
            ):
                raise RuntimeError("ALPHA source refresh Stage 2 failed unexpectedly")

        selection = _select_alpha_source_for_refresh(lock_conn, raw_file_ids)
        if selection is None:
            result.outcome = AlphaSourceRefreshOutcome.NO_NEWER_SOURCE_AVAILABLE
            return result

        identity = selection.identity
        if selection.requires_stage3:
            if not _invoke_stage(
                result.stage3, process_stage3_batch, Stage3BatchError,
                client, run_id, {"raw_file_id": identity.raw_file_id},
            ):
                raise RuntimeError("ALPHA source refresh Stage 3 failed unexpectedly")
            identities = result.stage3.result.successful_load_identities
            exact = [item for item in identities if (
                item.client_code == ALPHA_CLIENT_CODE
                and item.report_type == ALPHA_SOURCE_REPORT
                and item.destination_schema == "telematics_reports"
                and item.destination_table == ALPHA_SOURCE_REPORT
                and item.raw_file_id == identity.raw_file_id
            )]
            if len(exact) != 1:
                raise RuntimeError(
                    "ALPHA source refresh did not produce exactly one committed load identity"
                )
            identity = exact[0]
            result.source_load_committed = True

        result.successful_load_identity = identity
        result.normal_execute_postprocessor_outstanding = True
        plans, duplicates = _discover_postprocessor_plans(
            lock_conn, [identity],
            dependency_execution_mode=PostprocessorExecutionMode.DRY_RUN,
            dependency_only=True,
        )
        exact_plans = [plan for plan in plans if plan.postprocessor_name == ALPHA_DYSPONENT_POSTPROCESSOR]
        if duplicates or len(exact_plans) != 1 or len(plans) != 1:
            raise RuntimeError(
                "ALPHA source refresh requires exactly one static dry-run postprocessor plan"
            )
        result.postprocessor = _execute_postprocessor_plan(client, run_id, exact_plans[0])
        result.target_rows_modified = result.postprocessor.records_affected
        if result.target_rows_modified != 0:
            result.outcome = AlphaSourceRefreshOutcome.BLOCKED_TARGET_WRITE_DETECTED
            return result
        if result.postprocessor_dry_run_passed:
            result.outcome = (
                AlphaSourceRefreshOutcome.SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_PASSED
                if result.source_load_committed
                else AlphaSourceRefreshOutcome.ALREADY_LOADED
            )
        else:
            result.outcome = AlphaSourceRefreshOutcome.SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_FAILED
        return result
    finally:
        _relinquish_lock_session(client, run_id, lock_conn, acquired=result.lock_acquired)


def _invoke_stage(execution, function, typed_error, client, run_id, params) -> bool:
    execution.started = True
    try:
        execution.result = function(client, run_id, params)
        execution.completed = True
        execution.retryable_failure = bool(getattr(execution.result, "retryable_work_remains", False))
        execution.operator_action_required = bool(getattr(execution.result, "operator_action_required", False))
        return True
    except typed_error as exc:
        execution.result = exc.partial_result
        execution.completed = True
        execution.typed_partial_failure = True
        execution.retryable_failure = bool(getattr(execution.result, "retryable_work_remains", False))
        payload = execution.result.to_dict()
        execution.non_retryable_failure = bool(payload.get("non_retryable_failure_count") or payload.get("idempotency_conflict_count"))
        execution.operator_action_required = bool(getattr(execution.result, "operator_action_required", False))
        execution.error_category = "typed_partial_failure"
        return True
    except Exception as exc:
        # Bind the exception. Without this the single failure class that has no
        # typed partial result to fall back on also lost its type, message and
        # traceback, and every such incident read identically.
        execution.unexpected_failure = True
        execution.non_retryable_failure = True
        execution.error_category = "unexpected_stage_error"
        execution.exception_type = type(exc).__name__
        execution.exception_message = str(exc)[:500]
        execution.exception = exc
        # Capturing evidence must never become the failure. Each of these can
        # fail independently (a hostile __str__, an exhausted traceback), so
        # neither is allowed to escape and replace `exc`.
        try:
            execution.stack_trace = traceback.format_exc()
        except Exception:
            execution.stack_trace = None
        try:
            execution.origin_frames = origin_frames(exc)
        except Exception:
            execution.origin_frames = ()
        return False


# How many innermost frames are durable. Enough to name the failing call and its
# immediate callers; short enough that a deep recursive failure cannot bloat a
# log row or an email body.
MAX_ORIGIN_FRAMES = 6


def origin_frames(exc: BaseException, *, limit: int = MAX_ORIGIN_FRAMES) -> tuple[str, ...]:
    """Innermost frames of `exc` as bounded `path:line:function` strings.

    The *innermost* frames, not the outermost: the tail of a traceback is where
    the exception actually came from, and the head is the orchestrator calling
    itself, which the operator already knows.

    Paths are made repository-relative when possible. That is not cosmetic — an
    absolute path leaks the deployment layout and the release id into an email,
    and `/home/.../releases/<sha>/jobs/...` also makes two identical failures
    from different releases look different to a human reading them.

    Deliberately excluded: source text, local variables, arguments, and the
    exception repr (already carried separately as `exception_type` /
    `exception_message`).
    """
    frames = traceback.extract_tb(exc.__traceback__)
    out: list[str] = []
    for frame in frames[-limit:]:
        filename = frame.filename or "<unknown>"
        try:
            filename = str(Path(filename).relative_to(_REPO_ROOT))
        except (ValueError, OSError):
            filename = os.path.basename(filename)
        out.append(f"{filename}:{frame.lineno}:{frame.name}"[:200])
    return tuple(out)


def _dependency_postprocessor_params(
    conn,
    identity: Stage3SuccessfulLoadIdentity,
    execution_mode: PostprocessorExecutionMode = PostprocessorExecutionMode.EXECUTE,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                (current_row.stage3_finished_at AT TIME ZONE 'Europe/Warsaw')::date AS current_load_date,
                (
                    SELECT max(prior.stage3_finished_at AT TIME ZONE 'Europe/Warsaw')::date
                    FROM ingest.raw_file AS prior
                    WHERE prior.client_code = current_row.client_code
                      AND prior.stage2_report_type = current_row.stage2_report_type
                      AND prior.stage3_status = 'OK'
                      AND prior.id <> current_row.id
                      AND prior.stage3_finished_at IS NOT NULL
                      AND (prior.stage3_finished_at AT TIME ZONE 'Europe/Warsaw')::date
                          < (current_row.stage3_finished_at AT TIME ZONE 'Europe/Warsaw')::date
                ) AS previous_load_date
            FROM ingest.raw_file AS current_row
            WHERE current_row.id = %s
              AND current_row.client_code = %s
              AND current_row.stage2_report_type = %s
              AND current_row.stage3_status = 'OK'
              AND current_row.stage3_finished_at IS NOT NULL
            """,
            (identity.raw_file_id, identity.client_code, identity.report_type),
        )
        row = cur.fetchone()
    conn.rollback()
    if not row or row.get("current_load_date") is None:
        raise UnsupportedPostprocessorConfiguration(
            "committed Stage 3 load metadata is not available for dependency window resolution"
        )
    current_date = row["current_load_date"]
    previous_date = row.get("previous_load_date") or (current_date - timedelta(days=1))
    return {
        "client_code": identity.client_code,
        "dry_run": execution_mode is PostprocessorExecutionMode.DRY_RUN,
        "process_all": True,
        "date_from": previous_date.isoformat(),
        "date_to": current_date.isoformat(),
        "source_raw_file_id": identity.raw_file_id,
        "source_cleaned_artifact_id": identity.source_cleaned_artifact_id,
        "trigger": "workflow_b_committed_source_dependency",
    }


def _discover_postprocessor_plans(
    conn,
    identities: list[Stage3SuccessfulLoadIdentity],
    *,
    dependency_execution_mode: PostprocessorExecutionMode = PostprocessorExecutionMode.EXECUTE,
    dependency_only: bool = False,
) -> tuple[list[WorkflowBPostprocessorPlan], int]:
    plans: list[WorkflowBPostprocessorPlan] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    duplicates = 0

    def add(plan: WorkflowBPostprocessorPlan) -> None:
        nonlocal duplicates
        if plan.stable_identity in seen:
            duplicates += 1
            return
        seen.add(plan.stable_identity)
        plans.append(plan)

    for identity in identities:
        # P0-E. A reconciled identity means the load committed but the process
        # died somewhere after it, possibly before the postprocessor ran. Only a
        # postprocessor that has *declared* replay safety may be re-offered on
        # that basis; anything else would risk applying an irreversible effect
        # twice, and the safety is a property of each writer rather than a
        # blanket assumption about postprocessors in general.
        if identity.recovered:
            unsafe = [
                spec.name
                for spec in applicable_postprocessors(
                    client_code=identity.client_code,
                    report_type=identity.report_type,
                    destination_schema=identity.destination_schema,
                    destination_table=identity.destination_table,
                )
                if not spec.recovery_may_reoffer
            ]
            if unsafe:
                raise Stage3RecoveryReofferNotDeclaredSafe(identity, unsafe)

        for spec in dependency_postprocessors(
            client_code=identity.client_code,
            report_type=identity.report_type,
            destination_schema=identity.destination_schema,
            destination_table=identity.destination_table,
        ):
            try:
                resolve_postprocessor(
                    spec.name,
                    client_code=identity.client_code,
                    report_type=identity.report_type,
                    destination_schema=identity.destination_schema,
                    destination_table=identity.destination_table,
                    execution_mode=dependency_execution_mode,
                )
                parameters = _dependency_postprocessor_params(
                    conn, identity, dependency_execution_mode
                )
                error_category = None
            except UnsupportedPostprocessorConfiguration as exc:
                parameters = {}
                error_category = exc.code.lower()
            add(WorkflowBPostprocessorPlan(
                postprocessor_name=spec.name,
                raw_file_id=identity.raw_file_id,
                client_code=identity.client_code,
                report_type=identity.report_type,
                source_cleaned_artifact_id=identity.source_cleaned_artifact_id,
                selector="committed_stage3_dependency",
                selector_origin="static_registry",
                error_category=error_category,
                destination_schema=identity.destination_schema,
                destination_table=identity.destination_table,
                parameters=parameters,
                execution_mode=dependency_execution_mode,
            ))

        if dependency_only:
            continue

        with conn.cursor() as cur:
            state = load_report_policy_selector_state(
                cur, client_code=identity.client_code, report_type=identity.report_type
            )
        conn.rollback()
        try:
            resolution = resolve_workflow_b_trip_metrics_population_source(
                report_type=identity.report_type,
                client_default=state.client_default,
                report_override=state.report_override,
                load_policy_found=state.load_policy_found,
            )
        except WorkflowBSelectorResolutionError as exc:
            add(WorkflowBPostprocessorPlan(
                "unsupported_configured_postprocessor",
                identity.raw_file_id,
                identity.client_code,
                identity.report_type,
                identity.source_cleaned_artifact_id,
                "invalid",
                "unresolved",
                exc.category,
                identity.destination_schema,
                identity.destination_table,
            ))
            continue
        if resolution.compatibility in {
            SelectorCompatibility.SUPPORTED_DISABLED,
            SelectorCompatibility.SUPPORTED_WORKFLOW_A_SOURCE,
        }:
            continue
        postprocessor_name = (
            REPORT_207_POSTPROCESSOR
            if resolution.creates_postprocessor_plan
            else "unsupported_configured_postprocessor"
        )
        add(WorkflowBPostprocessorPlan(
            postprocessor_name,
            identity.raw_file_id,
            identity.client_code,
            identity.report_type,
            identity.source_cleaned_artifact_id,
            resolution.effective_selector,
            resolution.selector_origin.value,
            resolution.error_category,
            identity.destination_schema,
            identity.destination_table,
        ))
    return plans, duplicates


def _execute_postprocessor_plan(client, run_id, plan) -> WorkflowBPostprocessorResult:
    if plan.error_category and not plan.parameters and plan.postprocessor_name == ALPHA_DYSPONENT_POSTPROCESSOR:
        return WorkflowBPostprocessorResult(
            plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
            WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
            operator_action_required=True, error_category=plan.error_category,
            execution_mode=plan.execution_mode,
        )
    try:
        spec = resolve_postprocessor(
            plan.postprocessor_name,
            client_code=plan.client_code,
            report_type=plan.report_type,
            destination_schema=plan.destination_schema,
            destination_table=plan.destination_table,
            execution_mode=plan.execution_mode,
        )
    except UnsupportedPostprocessorConfiguration as exc:
        return WorkflowBPostprocessorResult(
            plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
            WorkflowBPostprocessorOutcome.BLOCKED_UNSUPPORTED_CONFIGURATION,
            operator_action_required=True, error_category=plan.error_category or exc.code.lower(),
            execution_mode=plan.execution_mode,
        )
    params = plan.parameters or {"client_code": plan.client_code, "raw_file_id": plan.raw_file_id}
    try:
        payload = spec.runner(client, run_id, params)
        summary = spec.result_adapter(payload)
        candidate_rows = int(summary.get("candidate_rows") or 0)
        migrated_rows = int(summary.get("migrated_rows") or 0)
        affected = int(summary.get("records_affected") or 0)
        outcome = (
            WorkflowBPostprocessorOutcome.SUCCEEDED
            if candidate_rows or migrated_rows or affected
            else WorkflowBPostprocessorOutcome.SKIPPED_ALREADY_COMPLETED
        )
        return WorkflowBPostprocessorResult(
            plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
            outcome,
            operator_action_required=bool(summary.get("operator_action_required")),
            candidate_rows=candidate_rows, migrated_rows=migrated_rows,
            records_affected=affected,
            execution_mode=plan.execution_mode,
            details=dict(summary.get("details") or {}),
        )
    except EnrichmentPreconditionError as exc:
        is_policy_failure = exc.code in {AMBIGUOUS_ENRICHMENT_MATCH, COVERAGE_BELOW_THRESHOLD}
        outcome = (
            WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE
            if is_policy_failure else WorkflowBPostprocessorOutcome.FAILED_RETRYABLE
        )
        return WorkflowBPostprocessorResult(
            plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
            outcome, retryable=not is_policy_failure,
            operator_action_required=is_policy_failure,
            error_category=exc.code.lower(), error_detail=str(exc),
            execution_mode=plan.execution_mode,
        )
    except PermissionError:
        outcome, category, retryable, operator = WorkflowBPostprocessorOutcome.FAILED_PERMISSION, "permission", False, True
        error_detail = "database permission denied"
    except Stage3SchemaReadinessError as exc:
        outcome, category, retryable, operator = WorkflowBPostprocessorOutcome.FAILED_SCHEMA_NOT_READY, "schema_not_ready", False, True
        error_detail = str(exc)
    except RuntimeSchemaMutationDisabledError as exc:
        outcome, category, retryable, operator = WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE, "runtime_schema_mutation_disabled", False, True
        error_detail = str(exc)
    except Exception as exc:
        name = type(exc).__name__
        if "EnvironmentIdentity" in name:
            outcome, category, retryable, operator = WorkflowBPostprocessorOutcome.FAILED_ENVIRONMENT_IDENTITY, "environment_identity", False, True
        else:
            outcome, category, retryable, operator = WorkflowBPostprocessorOutcome.FAILED_RETRYABLE, "postprocessor_execution", True, False
        error_detail = None
    return WorkflowBPostprocessorResult(
        plan.postprocessor_name, plan.raw_file_id, plan.client_code, plan.report_type,
        outcome, retryable=retryable, operator_action_required=operator,
        error_category=category, error_detail=error_detail,
        execution_mode=plan.execution_mode,
    )


def _finish_cycle(client, run_id: str, result: WorkflowBBatchResult) -> WorkflowBBatchResult:
    """Give every unresolved input its own signal, then settle the cycle.

    Placed on all four exits from the stage sequence rather than inside
    `_finish_or_raise`, because the signalling pass needs a platform connection
    and `_finish_or_raise` is a pure function over the result that tests call
    directly.

    Signalling runs *before* the outcome is settled, so a cycle that ends in
    `WorkflowBOrchestrationError` still names the inputs that need a human: one
    input's failure is not a reason to stay silent about another's.

    **It runs on its own connection, not the one holding the orchestration
    lock.** Signalling writes — incidents, occurrences, outbox rows — and a
    write that fails at connection level poisons the session it ran on. Sharing
    the lock session meant a signalling fault could break the one connection
    whose only real job is to hold `workflow_b.orchestrator.v1` until the cycle
    ends, and the failure then surfaced from cleanup instead of from here, where
    it is contained. Separate connection, separate blast radius; the lock
    session is never written through.

    A failure inside the alert path — including failing to open the connection
    at all — is logged and swallowed: reporting that an input is stuck must
    never become the reason a run is marked failed.

    **One exception, and it is a different statement.** If this cycle observed a
    *one-shot* unresolved input — one nothing will ever offer again — and the
    pass could establish neither an individual nor an aggregate durable incident
    for it, then the condition exists and no durable actionable representation
    of it does, anywhere. That is not "reporting failed"; it is "the arrived
    input is now invisible", and a cycle that settles as healthy or as a review
    outcome would be asserting coverage that does not exist. Only that flag
    reaches `_finish_or_raise`, which turns it into an ordinary terminal
    Workflow B failure on the existing JOB_TERMINAL_FAILURE path. Every other
    signalling fault — including on a cycle whose unresolved inputs are all
    replayable — is contained exactly as before.
    """
    signal_conn = None
    try:
        signal_conn = _platform_pg_conn()
        result.unresolved_input_signal = signal_unresolved_inputs(client, signal_conn, run_id, result)
        result.unresolved_signal_safety_failure = (
            result.unresolved_input_signal.safety_contract_violated
        )
    except Exception as exc:
        result.unresolved_input_signal = None
        # The pass did not run, so it proved nothing. Whether that matters is a
        # question about the cycle's own in-memory observations, and collection
        # is pure and needs no database: if this cycle saw one-shot unresolved
        # work, then nothing durable represents it and the cycle must not
        # settle. If it saw none, an unreachable alert path is contained exactly
        # as before and the outcome is untouched.
        result.unresolved_signal_safety_failure = _observed_one_shot_unresolved(result)
        try:
            client.log(
                "ERROR" if result.unresolved_signal_safety_failure else "WARNING",
                "SCRIPT", JOB_SOURCE,
                "Workflow B unresolved-input signalling failed",
                run_id=run_id,
                context={"exception_type": type(exc).__name__, "exception_message": str(exc)[:500],
                         "unresolved_signal_safety_failure":
                             result.unresolved_signal_safety_failure},
            )
        except Exception:
            pass
    finally:
        if signal_conn is not None:
            try:
                signal_conn.close()
            except Exception:
                pass
    return _finish_or_raise(result)


def _observed_one_shot_unresolved(result: WorkflowBBatchResult) -> bool:
    """Did this cycle observe unresolved work no later cycle will offer again?

    Only used when the signalling pass itself failed. `collect_unresolved_inputs`
    is pure over the in-memory batch result, so it can answer here; if even that
    raises, the honest answer is that the question could not be settled, and the
    fail-safe direction for a safety mechanism is to assume the worst rather
    than to report coverage it cannot demonstrate.
    """
    try:
        return one_shot_unresolved_input_count(result) > 0
    except Exception:
        return True


def _assert_mailbox_was_inspected(result: WorkflowBBatchResult) -> None:
    """A cycle that never looked in the mailbox cannot report healthy no-work.

    Stage 1 raises on every mailbox-access failure it knows about — login,
    SELECT, SEARCH, FETCH — so today this cannot trigger. It is asserted anyway
    because the operating contract turns on it: `SUCCEEDED_NO_WORK` is the one
    outcome that claims "we looked and there was nothing", and it is the single
    most expensive claim in the workflow to get wrong. A future Stage 1 that
    returned a clean empty result without reaching `IMAP SEARCH` would otherwise
    be indistinguishable from an empty mailbox, forever.

    Classified retryable: an unreachable or unreadable mailbox is an
    infrastructure condition, and the next 06:00/20:00 cycle is the right place
    to find out whether it cleared.
    """
    stage1 = result.stage1
    if not (stage1.started and stage1.completed) or stage1.failed:
        return
    native = stage1.result
    if not isinstance(native, Stage1BatchResult) or native.mailbox_check_completed:
        return
    stage1.retryable_failure = True
    stage1.error_category = "mailbox_check_not_completed"


def _finish_or_raise(result: WorkflowBBatchResult) -> WorkflowBBatchResult:
    _assert_mailbox_was_inspected(result)
    if any(stage.unexpected_failure for stage in result.stages):
        result.outcome = WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR
    elif result.retryable_failure_count:
        result.outcome = WorkflowBOutcome.FAILED_RETRYABLE
    elif result.non_retryable_failure_count:
        result.outcome = WorkflowBOutcome.FAILED_NON_RETRYABLE
    elif result.unresolved_signal_safety_failure:
        # Fail closed, and only here. An input arrived, this cycle is the only
        # place its unresolved condition was ever observable, and the signalling
        # pass established neither an individual nor an aggregate durable
        # incident for it. Settling as SUCCEEDED_WITH_REVIEW_ITEMS would assert
        # that the condition is represented somewhere an operator will find it,
        # which is exactly what is false. Non-retryable because re-running
        # cannot recover evidence that no longer exists; the run therefore ends
        # FAILED and `ops/runner.py` raises the existing JOB_TERMINAL_FAILURE
        # incident, which is the durable independently-actionable path this
        # needs — no second alerting mechanism is introduced.
        #
        # Deliberately *below* the stage failures: a cycle that already failed
        # for a stage reason is already durably actionable, and renaming its
        # cause would lose the origin the operator needs.
        result.outcome = WorkflowBOutcome.FAILED_NON_RETRYABLE
    elif result.operator_action_required:
        result.outcome = WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS
    elif result.useful_work_occurred:
        result.outcome = WorkflowBOutcome.SUCCEEDED
    else:
        result.outcome = WorkflowBOutcome.SUCCEEDED_NO_WORK
    if result.outcome.value.startswith("FAILED_") or result.outcome == WorkflowBOutcome.BLOCKED_OPERATOR_ACTION:
        # Chain the originating stage exception. `ops/runner.py` captures
        # `traceback.format_exc()` at its own boundary, and without `from` that
        # traceback shows only this raise — the operator sees Workflow B failing
        # at `_finish_or_raise` and nothing about where the ValueError came from.
        # Chaining makes the existing (already sanitized and length-bounded)
        # incident `stack_trace` carry the origin, with no new field and no new
        # transport.
        stage = result.failing_stage
        origin = stage.exception if stage is not None else None
        if origin is not None:
            raise WorkflowBOrchestrationError(result) from origin
        # `raise ... from None` is NOT equivalent to a bare raise: it sets
        # __suppress_context__ and would hide the implicit chaining Python does
        # for free. Typed partial failures have no captured origin, so they must
        # keep the plain form.
        raise WorkflowBOrchestrationError(result)
    return result


def _safe_stage_summary(native_result: Any) -> dict[str, Any] | None:
    if native_result is None:
        return None
    value = native_result.to_dict()
    for key in ("items", "artifact_reconciliation", "successful_load_identities"):
        value.pop(key, None)
    return value


def run(client, run_id: str, params: dict) -> WorkflowBBatchResult:
    """Execute one Workflow B cycle and always persist its terminal payload.

    The summary used to be logged only after a successful return, so a failed run
    — the one an operator actually needs to reconstruct — persisted no structured
    outcome at all: `public.logs` held the exception string and nothing else.
    Logging on both paths costs one row and is what makes a failed cycle
    diagnosable after journald has rotated.

    The original exception stays authoritative: it is re-raised unchanged, and a
    logging failure here is swallowed rather than allowed to replace it.
    """
    try:
        result = run_workflow_b_batch(client, run_id, params)
    except WorkflowBOrchestrationError as exc:
        _log_terminal_result(client, run_id, exc.partial_result, level="ERROR")
        raise
    _log_terminal_result(client, run_id, result, level="INFO")
    return result


def _log_terminal_result(client, run_id: str, result: WorkflowBBatchResult, *, level: str) -> None:
    try:
        client.log(
            level, "SCRIPT", JOB_SOURCE, "Workflow B orchestration finished",
            run_id=run_id, context=result.to_dict(),
        )
    except Exception:
        # Never let observability convert one failure into a different one.
        pass
