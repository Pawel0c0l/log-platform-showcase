#!/usr/bin/env python3
"""Service-free Workflow B report-selector migration, resolver, and matrix tests."""
from __future__ import annotations

import json
from pathlib import Path

from jobs.reports.workflow_b.trip_metrics_selector import (
    SelectorCompatibility,
    SelectorOrigin,
    WorkflowBSelectorResolutionError,
    resolve_workflow_b_trip_metrics_population_source,
    validate_report_policy_selector_override,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db/migrations/050_workflow_b_report_postprocessor_selector_override.sql"


def resolve(report_type, client="api_migration", override=None, found=True):
    return resolve_workflow_b_trip_metrics_population_source(
        report_type=report_type,
        client_default=client,
        report_override=override,
        load_policy_found=found,
    )


def expect_error(category, **kwargs):
    try:
        resolve(**kwargs)
    except WorkflowBSelectorResolutionError as exc:
        assert exc.category == category
        assert exc.operator_action_required is True
    else:
        raise AssertionError(f"expected selector error {category}")


def test_migration_contract() -> None:
    sql = MIGRATION.read_text()
    assert "ops_control.environment_identity" in sql
    assert "ADD COLUMN IF NOT EXISTS trip_metrics_population_source_override TEXT" in sql
    assert "ck_report_type_client_load_policy_trip_metrics_source_override" in sql
    assert "SET DEFAULT" not in sql and "UPDATE workflow_b_control" not in sql
    assert "CREATE INDEX" not in sql
    for value in ("api_migration", "report_207_migration", "d105_2_ecodriving_migration", "disabled"):
        assert f"'{value}'" in sql


def test_resolution_and_serialization() -> None:
    inherited = resolve("report_207", client="report_207_migration")
    assert inherited.effective_selector == "report_207_migration"
    assert inherited.selector_origin == SelectorOrigin.CLIENT_DEFAULT
    overridden = resolve("report_207", client="api_migration", override="report_207_migration")
    assert overridden.selector_origin == SelectorOrigin.REPORT_POLICY_OVERRIDE
    assert overridden.client_default == "api_migration"
    assert overridden.report_override == "report_207_migration"
    payload = json.loads(json.dumps(overridden.to_dict()))
    assert payload["selector_origin"] == "report_policy_override"
    assert "client_code" not in payload

    for value in (None, "api_migration", "report_207_migration", "d105_2_ecodriving_migration", "disabled"):
        assert validate_report_policy_selector_override(value) == value
    for value, category in (("", "malformed_report_override"), (" ", "malformed_report_override"), (" disabled ", "malformed_report_override"), ("unknown", "unknown_report_override")):
        try:
            validate_report_policy_selector_override(value)
        except WorkflowBSelectorResolutionError as exc:
            assert exc.category == category
        else:
            raise AssertionError(f"invalid override accepted: {value!r}")
    expect_error("unknown_client_default", report_type="report_207", client="unknown")
    expect_error("malformed_client_default", report_type="report_207", client=" ")
    expect_error("missing_report_policy", report_type="report_207", found=False)


def test_compatibility_matrix() -> None:
    assert resolve("report_207", client="report_207_migration").compatibility == SelectorCompatibility.SUPPORTED_POSTPROCESSOR
    assert resolve("report_207", override="disabled").compatibility == SelectorCompatibility.SUPPORTED_DISABLED
    assert resolve("report_207", client="api_migration").compatibility == SelectorCompatibility.SUPPORTED_WORKFLOW_A_SOURCE
    assert resolve("report_207", override="d105_2_ecodriving_migration").compatibility == SelectorCompatibility.INCOMPATIBLE

    assert resolve("Alpha_GPS_Baza_LOG", override="disabled").compatibility == SelectorCompatibility.SUPPORTED_DISABLED
    assert resolve("Alpha_GPS_Baza_LOG", override="report_207_migration").compatibility == SelectorCompatibility.INCOMPATIBLE
    assert resolve("Alpha_GPS_Baza_LOG", override="d105_2_ecodriving_migration").compatibility == SelectorCompatibility.INCOMPATIBLE
    assert resolve("Alpha_GPS_Baza_LOG", override="api_migration").compatibility == SelectorCompatibility.SUPPORTED_WORKFLOW_A_SOURCE

    d105 = resolve("report_d105_2_ecodriving", override="d105_2_ecodriving_migration")
    assert d105.compatibility == SelectorCompatibility.PARENT_UNSUPPORTED
    assert d105.operator_action_required and not d105.creates_postprocessor_plan
    assert resolve("unrelated_report", override="disabled").compatibility == SelectorCompatibility.SUPPORTED_DISABLED


def main() -> None:
    test_migration_contract()
    test_resolution_and_serialization()
    test_compatibility_matrix()
    print("OK - Workflow B report selector override checks passed")


if __name__ == "__main__":
    main()
