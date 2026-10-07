from __future__ import annotations

"""Static, applicability-checked Workflow B postprocessor registry."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from jobs.reports.postprocess import (
    job_alpha00001_dysponent_id_enrichment,
    job_report_207_speeding_migration,
)

REPORT_207_POSTPROCESSOR = "report_207_speeding_migration"
ALPHA_DYSPONENT_POSTPROCESSOR = "alpha00001_dysponent_id_enrichment"
ALPHA_CLIENT_CODE = "ALPHA00001"
ALPHA_SOURCE_REPORT = "Alpha_GPS_Baza_LOG"


class UnsupportedPostprocessorConfiguration(ValueError):
    code = "UNSUPPORTED_POSTPROCESSOR_CONFIGURATION"


class PostprocessorExecutionMode(StrEnum):
    EXECUTE = "execute"
    DRY_RUN = "dry_run"


class PostprocessorRecoverySafety(StrEnum):
    """Whether Stage 3 crash recovery may re-offer this postprocessor (P0-E).

    Recovery reconciles a load that committed before the process died, and that
    crash may have happened *before* the postprocessor ran. Re-offering the
    identity is therefore how the skipped postprocessing is closed — but only
    where re-running provably cannot apply an irreversible effect twice.

    This is declared per postprocessor rather than assumed, because the two
    current ones are safe for **different** reasons and a third could be safe
    for neither:

      * `report_207_speeding_migration` **increments** speeding counters, so it
        is not convergent. It is safe only because selection and marking are one
        statement: candidates are `COALESCE(migrated_to_client_db, FALSE) IS NOT
        TRUE`, and `updated_trips` (the increment) and `marked_migrated` (the
        flag) are CTEs of a single UPDATE, so a row is either incremented and
        marked or neither. An already-migrated row is never re-selected.
      * `alpha00001_dysponent_id_enrichment` **sets** a value and is convergent:
        `planned_updates` counts only trips whose `dysponent_id` is still NULL
        (or differs under an explicit overwrite), so a second run affects zero
        rows.

    Anything not explicitly declared safe is `UNKNOWN` and is never re-offered.
    """

    #: Re-running after a reconciled load cannot duplicate an irreversible
    #: effect. The `justification` field records why, per spec.
    SAFE_TO_REOFFER = "safe_to_reoffer"
    #: Not established. Recovery must not re-offer; an operator decides.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PostprocessorSpec:
    name: str
    report_type: str
    client_code: str | None
    runner: Callable[[Any, str, dict[str, Any]], Any]
    result_adapter: Callable[[Any], dict[str, Any]]
    destination_schema: str
    destination_table: str
    supported_modes: frozenset[PostprocessorExecutionMode]
    dependency_coupled: bool = False
    #: P0-E. Fail-closed default: a newly added postprocessor does not inherit
    #: recovery-safe status by omission.
    recovery_safety: PostprocessorRecoverySafety = PostprocessorRecoverySafety.UNKNOWN
    #: Why `recovery_safety` holds, in the terms of the actual writer. Required
    #: whenever the safety is not UNKNOWN, and asserted by the test suite.
    recovery_safety_justification: str = ""

    @property
    def recovery_may_reoffer(self) -> bool:
        return self.recovery_safety is PostprocessorRecoverySafety.SAFE_TO_REOFFER

    def applies_to(
        self,
        *,
        client_code: str,
        report_type: str,
        destination_schema: str | None = None,
        destination_table: str | None = None,
    ) -> bool:
        return (
            self.report_type == report_type
            and (self.client_code is None or self.client_code == client_code)
            and (destination_schema is None or self.destination_schema == destination_schema)
            and (destination_table is None or self.destination_table == destination_table)
        )


def _report_207_result(payload: Any) -> dict[str, Any]:
    summaries = payload.get("clients") or []
    summary = summaries[0] if summaries else {}
    candidate_rows = int(summary.get("candidate_rows") or 0)
    migrated_rows = int(summary.get("migrated_rows") or 0)
    return {
        "candidate_rows": candidate_rows,
        "migrated_rows": migrated_rows,
        "records_affected": migrated_rows,
        "operator_action_required": summary.get("status") == "WARNING",
    }


def _alpha_dysponent_result(payload: Any) -> dict[str, Any]:
    return {
        "candidate_rows": int(payload.get("target_trips_in_scope") or 0),
        "migrated_rows": int(payload.get("rows_updated") or 0),
        "records_affected": int(payload.get("rows_updated") or 0),
        "operator_action_required": not bool(payload.get("readiness_passed")),
        "details": dict(payload),
    }


POSTPROCESSOR_REGISTRY: dict[str, PostprocessorSpec] = {
    REPORT_207_POSTPROCESSOR: PostprocessorSpec(
        REPORT_207_POSTPROCESSOR,
        "report_207",
        None,
        job_report_207_speeding_migration.run,
        _report_207_result,
        "telematics_reports",
        "report_207",
        frozenset({PostprocessorExecutionMode.EXECUTE}),
        recovery_safety=PostprocessorRecoverySafety.SAFE_TO_REOFFER,
        recovery_safety_justification=(
            "not convergent - it increments speeding counters. Safe only because "
            "candidate selection is COALESCE(migrated_to_client_db, FALSE) IS NOT TRUE "
            "and the increment (updated_trips) and the marker (marked_migrated) are "
            "CTEs of one statement, so an already-migrated row is never re-selected "
            "and a partially applied row cannot exist. The orchestrator never sets "
            "force_retry_errors, so the marker always governs the autonomous path."
        ),
    ),
    ALPHA_DYSPONENT_POSTPROCESSOR: PostprocessorSpec(
        ALPHA_DYSPONENT_POSTPROCESSOR,
        ALPHA_SOURCE_REPORT,
        ALPHA_CLIENT_CODE,
        job_alpha00001_dysponent_id_enrichment.run,
        _alpha_dysponent_result,
        "telematics_reports",
        ALPHA_SOURCE_REPORT,
        frozenset({PostprocessorExecutionMode.EXECUTE, PostprocessorExecutionMode.DRY_RUN}),
        dependency_coupled=True,
        recovery_safety=PostprocessorRecoverySafety.SAFE_TO_REOFFER,
        recovery_safety_justification=(
            "convergent: planned_updates counts only trips whose dysponent_id is still "
            "NULL (or differs under an explicit overwrite), and the write sets a value "
            "rather than accumulating one, so a second run affects zero rows and "
            "reports SKIPPED_ALREADY_COMPLETED."
        ),
    ),
}


def resolve_postprocessor(
    name: str,
    *,
    client_code: str,
    report_type: str,
    destination_schema: str | None = None,
    destination_table: str | None = None,
    execution_mode: PostprocessorExecutionMode | str = PostprocessorExecutionMode.EXECUTE,
) -> PostprocessorSpec:
    try:
        mode = PostprocessorExecutionMode(execution_mode)
    except (TypeError, ValueError) as exc:
        raise UnsupportedPostprocessorConfiguration(
            f"unknown Workflow B postprocessor execution mode: {execution_mode!r}"
        ) from exc
    spec = POSTPROCESSOR_REGISTRY.get(name)
    if spec is None:
        raise UnsupportedPostprocessorConfiguration(
            f"unknown Workflow B postprocessor: {name!r}"
        )
    if not spec.applies_to(
        client_code=client_code,
        report_type=report_type,
        destination_schema=destination_schema,
        destination_table=destination_table,
    ):
        raise UnsupportedPostprocessorConfiguration(
            f"postprocessor {name!r} is not applicable to "
            f"client={client_code!r}, report_type={report_type!r}"
        )
    if mode not in spec.supported_modes:
        raise UnsupportedPostprocessorConfiguration(
            f"postprocessor {name!r} does not support execution mode {mode.value!r}"
        )
    return spec


def dependency_postprocessors(
    *,
    client_code: str,
    report_type: str,
    destination_schema: str,
    destination_table: str,
) -> tuple[PostprocessorSpec, ...]:
    return tuple(
        spec
        for spec in POSTPROCESSOR_REGISTRY.values()
        if spec.dependency_coupled
        and spec.applies_to(
            client_code=client_code,
            report_type=report_type,
            destination_schema=destination_schema,
            destination_table=destination_table,
        )
    )


def applicable_postprocessors(
    *,
    client_code: str,
    report_type: str,
    destination_schema: str,
    destination_table: str,
) -> tuple[PostprocessorSpec, ...]:
    """Every registered postprocessor that could act on this identity.

    P0-E. Broader than `dependency_postprocessors`, which filters to
    `dependency_coupled`. Recovery has to consider *all* of them, because the
    question is not "which run as a dependency" but "which could this recovered
    identity cause to run at all".
    """
    return tuple(
        spec
        for spec in POSTPROCESSOR_REGISTRY.values()
        if spec.applies_to(
            client_code=client_code,
            report_type=report_type,
            destination_schema=destination_schema,
            destination_table=destination_table,
        )
    )
