from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from jobs.trip_metrics_population_source import (
    TRIP_METRICS_POPULATION_SOURCE_VALUES,
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    TRIP_METRICS_SOURCE_DISABLED,
    TRIP_METRICS_SOURCE_REPORT_207,
)

REPORT_207 = "report_207"
REPORT_D105_2_ECODRIVING = "report_d105_2_ecodriving"


class SelectorOrigin(StrEnum):
    REPORT_POLICY_OVERRIDE = "report_policy_override"
    CLIENT_DEFAULT = "client_default"


class SelectorCompatibility(StrEnum):
    SUPPORTED_POSTPROCESSOR = "supported_postprocessor"
    SUPPORTED_DISABLED = "supported_disabled"
    SUPPORTED_WORKFLOW_A_SOURCE = "supported_workflow_a_source"
    PARENT_UNSUPPORTED = "parent_unsupported"
    INCOMPATIBLE = "incompatible"


class WorkflowBSelectorResolutionError(ValueError):
    def __init__(self, category: str, message: str):
        self.category = category
        self.operator_action_required = True
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReportLoadPolicySelectorState:
    client_default: Any
    report_override: Any
    load_policy_found: bool


@dataclass(frozen=True, slots=True)
class WorkflowBTripMetricsSelectorResolution:
    effective_selector: str
    selector_origin: SelectorOrigin
    client_default: str
    report_override: str | None
    compatibility: SelectorCompatibility
    operator_action_required: bool
    error_category: str | None

    @property
    def creates_postprocessor_plan(self) -> bool:
        return self.compatibility == SelectorCompatibility.SUPPORTED_POSTPROCESSOR

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["selector_origin"] = self.selector_origin.value
        value["compatibility"] = self.compatibility.value
        return value


def validate_report_policy_selector_override(value: object) -> str | None:
    if value is None:
        return None
    return _validate_selector(value, error_prefix="report_override")


def _validate_selector(value: object, *, error_prefix: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkflowBSelectorResolutionError(
            f"malformed_{error_prefix}", f"{error_prefix} selector is malformed"
        )
    if value not in TRIP_METRICS_POPULATION_SOURCE_VALUES:
        raise WorkflowBSelectorResolutionError(
            f"unknown_{error_prefix}", f"{error_prefix} selector is unknown"
        )
    return value


def resolve_workflow_b_trip_metrics_population_source(
    *,
    report_type: str,
    client_default: object,
    report_override: object,
    load_policy_found: bool,
) -> WorkflowBTripMetricsSelectorResolution:
    if not load_policy_found:
        raise WorkflowBSelectorResolutionError(
            "missing_report_policy", "Workflow B report load policy is missing"
        )
    client_selector = _validate_selector(client_default, error_prefix="client_default")
    override = validate_report_policy_selector_override(report_override)
    effective = override if override is not None else client_selector
    origin = (
        SelectorOrigin.REPORT_POLICY_OVERRIDE
        if override is not None
        else SelectorOrigin.CLIENT_DEFAULT
    )

    if effective == TRIP_METRICS_SOURCE_DISABLED:
        compatibility = SelectorCompatibility.SUPPORTED_DISABLED
        error_category = None
    elif effective == TRIP_METRICS_SOURCE_API:
        compatibility = SelectorCompatibility.SUPPORTED_WORKFLOW_A_SOURCE
        error_category = None
    elif report_type == REPORT_207 and effective == TRIP_METRICS_SOURCE_REPORT_207:
        compatibility = SelectorCompatibility.SUPPORTED_POSTPROCESSOR
        error_category = None
    elif (
        report_type == REPORT_D105_2_ECODRIVING
        and effective == TRIP_METRICS_SOURCE_D105_2_ECODRIVING
    ):
        compatibility = SelectorCompatibility.PARENT_UNSUPPORTED
        error_category = "parent_unsupported_selector"
    else:
        compatibility = SelectorCompatibility.INCOMPATIBLE
        error_category = "incompatible_report_selector"

    operator_action = error_category is not None
    return WorkflowBTripMetricsSelectorResolution(
        effective_selector=effective,
        selector_origin=origin,
        client_default=client_selector,
        report_override=override,
        compatibility=compatibility,
        operator_action_required=operator_action,
        error_category=error_category,
    )


def load_report_policy_selector_state(
    cur, *, client_code: str, report_type: str
) -> ReportLoadPolicySelectorState:
    cur.execute(
        """
        SELECT
            account.trip_metrics_population_source AS client_default,
            policy.trip_metrics_population_source_override AS report_override
        FROM workflow_b_control.report_type_client_load_policy AS policy
        JOIN workflow_a_control.client_account AS account
          ON account.client_code = policy.client_code
         AND account.enabled IS TRUE
        WHERE policy.client_code = %s
          AND policy.report_type = %s
        """,
        (client_code, report_type),
    )
    row = cur.fetchone()
    if not row:
        return ReportLoadPolicySelectorState(None, None, False)
    return ReportLoadPolicySelectorState(
        client_default=row.get("client_default"),
        report_override=row.get("report_override"),
        load_policy_found=True,
    )
