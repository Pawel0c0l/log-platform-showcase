#!/usr/bin/env python3
"""Service-free parent Workflow B orchestrator regressions."""
from __future__ import annotations

import json
import sys
import types
from dataclasses import replace
from datetime import date

try:
    import pandas  # noqa: F401
except ModuleNotFoundError:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = type("DataFrame", (), {})
    pandas_stub.Series = type("Series", (), {})
    sys.modules["pandas"] = pandas_stub

from jobs.mail.stage1_batch_contract import Stage1BatchError, Stage1BatchResult, Stage1ItemResult, Stage1Outcome
from jobs.reports.stage2.batch_contract import Stage2BatchError, Stage2BatchResult, Stage2ItemResult, Stage2Outcome
from jobs.reports.stage3.batch_contract import Stage3BatchError, Stage3BatchResult, Stage3ItemResult, Stage3Outcome
from jobs.reports.stage3.schema_readiness import MissingSchemaObject, Stage3SchemaReadinessError
from jobs.reports.workflow_b import orchestrator as job
from jobs.reports.workflow_b import postprocessor_registry as registry


RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"


class Cursor:
    def __init__(self, conn): self.conn = conn; self.row = None
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def execute(self, sql, params=()):
        self.conn.sql.append((sql, params))
        if "pg_try_advisory_lock" in sql: self.row = {"pg_try_advisory_lock": self.conn.locked}
        elif "pg_advisory_unlock" in sql: self.row = {"pg_advisory_unlock": True}
        elif "current_load_date" in sql:
            self.row = {"current_load_date": date(2026, 7, 23), "previous_load_date": date(2026, 7, 21)}
        elif "trip_metrics_population_source_override" in sql:
            self.row = (
                None
                if not self.conn.policy_found
                else {
                    "client_default": self.conn.selector,
                    "report_override": self.conn.override,
                }
            )
    def fetchone(self): return self.row


class Conn:
    def __init__(self, locked=True, selector="api_migration", override=None, policy_found=True):
        self.locked = locked
        self.selector = selector
        self.override = override
        self.policy_found = policy_found
        self.sql = []
        self.closed = False
    def cursor(self): return Cursor(self)
    def commit(self): return None
    def rollback(self): return None
    def close(self): self.closed = True


class Client:
    def __init__(self): self.logs = []
    def log(self, *args, **kwargs): self.logs.append((args, kwargs))


class Patch:
    def __init__(self, **values): self.values = values; self.originals = {}
    def __enter__(self):
        for name, value in self.values.items(): self.originals[name] = getattr(job, name); setattr(job, name, value)
    def __exit__(self, *_args):
        for name, value in self.originals.items(): setattr(job, name, value)


def s1_zero(): return Stage1BatchResult(mailbox_check_completed=True)
def s2_zero(): return Stage2BatchResult()
def s3_zero(): return Stage3BatchResult()
def s3_loaded():
    return Stage3BatchResult(discovered_candidate_count=1, eligible_count=1, items=[
        Stage3ItemResult(RAW_ID, "CLIENT", "report_207", Stage3Outcome.LOADED,
                         destination_schema="telematics_reports", destination_table="report_207",
                         persisted_status="OK", source_cleaned_artifact_id="artifact")
    ])


def test_lock_and_healthy_sequence() -> None:
    calls = []
    conn = Conn()
    def one(client, run_id, params): calls.append(("stage1", client, run_id)); return s1_zero()
    def two(client, run_id, params): calls.append(("stage2", client, run_id)); return s2_zero()
    def three(client, run_id, params): calls.append(("stage3", client, run_id)); return s3_zero()
    client = Client()
    with Patch(_platform_pg_conn=lambda: conn, fetch_reports_batch=one, process_stage2_batch=two, process_stage3_batch=three):
        result = job.run_workflow_b_batch(client, "parent", {})
    assert [x[0] for x in calls] == ["stage1", "stage2", "stage3"]
    assert all(x[1] is client and x[2] == "parent" for x in calls)
    assert result.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.sql)
    assert conn.closed

    # P0-D. Losing the lock never runs a stage — that half is unchanged, and the
    # AssertionError stage stubs below are what prove it in both modes.
    never = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())

    # A *scheduled* fire that loses the lock did not execute the required cycle.
    # It must raise, so `run_context` records the run FAILED and the watchdog
    # never counts it as a satisfied fire.
    loser = Conn(locked=False)
    with Patch(_platform_pg_conn=lambda: loser, fetch_reports_batch=never,
               process_stage2_batch=never, process_stage3_batch=never):
        try:
            job.run_workflow_b_batch(client, "parent", {})
        except job.WorkflowBOrchestrationError as exc:
            blocked = exc.partial_result
        else:
            raise AssertionError("a scheduled lock loss must not return successfully")
    assert blocked.outcome == job.WorkflowBOutcome.BLOCKED_CONCURRENT_EXECUTION
    assert not blocked.lock_acquired and not blocked.cycle_executed
    assert loser.closed
    # The operator alert must say a required cycle was skipped and that nothing
    # was written, which is what makes a manual re-run obviously safe.
    details = blocked.incident_details()
    assert details["cycle_executed"] is False
    assert details["durable_writes_committed"] is False
    assert details["lock_namespace"] == job.WORKFLOW_B_LOCK_NAMESPACE

    # A manual_diagnostic run losing to the scheduled owner is the intended
    # ownership model: silent, exit 0, no incident.
    manual_loser = Conn(locked=False)
    with Patch(_platform_pg_conn=lambda: manual_loser, fetch_reports_batch=never,
               process_stage2_batch=never, process_stage3_batch=never):
        skipped = job.run_workflow_b_batch(client, "parent", {"mode": "manual_diagnostic"})
    assert skipped.outcome == job.WorkflowBOutcome.SKIPPED_LOCKED
    assert not skipped.lock_acquired and not skipped.cycle_executed

    # And the distinction P0-D exists for: a genuine no-work cycle *did* execute.
    assert result.outcome == job.WorkflowBOutcome.SUCCEEDED_NO_WORK
    assert result.cycle_executed and result.successful_zero_work


def test_typed_continuation_and_unexpected_stop() -> None:
    calls = []
    partial1 = Stage1BatchResult(mailbox_check_completed=False, items=[
        Stage1ItemResult(Stage1Outcome.FAILED_RETRYABLE_MAIL_FETCH, retryable=True)
    ])
    with Patch(
        _platform_pg_conn=lambda: Conn(),
        fetch_reports_batch=lambda *_a, **_k: (_ for _ in ()).throw(Stage1BatchError(partial1)),
        process_stage2_batch=lambda *_a, **_k: calls.append("stage2") or s2_zero(),
        process_stage3_batch=lambda *_a, **_k: calls.append("stage3") or s3_zero(),
    ):
        try: job.run_workflow_b_batch(Client(), "parent", {})
        except job.WorkflowBOrchestrationError as exc: result = exc.partial_result
        else: raise AssertionError("typed Stage 1 failure was accepted")
    assert calls == ["stage2", "stage3"] and result.stage1.typed_partial_failure

    calls.clear()
    with Patch(
        _platform_pg_conn=lambda: Conn(),
        fetch_reports_batch=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unexpected")),
        process_stage2_batch=lambda *_a, **_k: calls.append("stage2"),
        process_stage3_batch=lambda *_a, **_k: calls.append("stage3"),
    ):
        try: job.run_workflow_b_batch(Client(), "parent", {})
        except job.WorkflowBOrchestrationError as exc: result = exc.partial_result
        else: raise AssertionError("unexpected Stage 1 failure was accepted")
    assert calls == [] and result.outcome == job.WorkflowBOutcome.FAILED_UNEXPECTED_STAGE_ERROR


def test_stage2_and_stage3_partial_continuation() -> None:
    partial2 = Stage2BatchResult(items=[Stage2ItemResult(RAW_ID, Stage2Outcome.FAILED_RETRYABLE, retryable=True)])
    partial3 = s3_loaded()
    partial3.items.append(Stage3ItemResult("raw-fail", "CLIENT", "report_207", Stage3Outcome.FAILED_DATABASE_LOAD, retryable=True))
    post = []
    with Patch(
        _platform_pg_conn=lambda: Conn(selector="report_207_migration"),
        fetch_reports_batch=lambda *_a, **_k: s1_zero(),
        process_stage2_batch=lambda *_a, **_k: (_ for _ in ()).throw(Stage2BatchError(partial2)),
        process_stage3_batch=lambda *_a, **_k: (_ for _ in ()).throw(Stage3BatchError(partial3)),
        _execute_postprocessor_plan=lambda *_a, **_k: post.append(True) or job.WorkflowBPostprocessorResult(
            job.REPORT_207_POSTPROCESSOR, RAW_ID, "CLIENT", "report_207", job.WorkflowBPostprocessorOutcome.SUCCEEDED, records_affected=1),
    ):
        try: job.run_workflow_b_batch(Client(), "parent", {})
        except job.WorkflowBOrchestrationError as exc: result = exc.partial_result
        else: raise AssertionError("partial failures were accepted")
    assert post == [True] and result.stage2.typed_partial_failure and result.stage3.typed_partial_failure
    assert result.postprocessor_successes == 1


def test_postprocessor_selection_dedup_and_execution() -> None:
    identity = s3_loaded().successful_load_identities[0]
    plans, duplicates = job._discover_postprocessor_plans(Conn(selector="report_207_migration"), [identity, identity])
    assert len(plans) == 1 and duplicates == 1
    assert plans[0].raw_file_id == RAW_ID and plans[0].selector == "report_207_migration"
    assert plans[0].selector_origin == "client_default"

    override_plans, _ = job._discover_postprocessor_plans(
        Conn(selector="api_migration", override="report_207_migration"), [identity]
    )
    assert override_plans[0].selector_origin == "report_policy_override"
    assert job._discover_postprocessor_plans(Conn(selector="api_migration"), [identity]) == ([], 0)
    assert job._discover_postprocessor_plans(
        Conn(selector="report_207_migration", override="disabled"), [identity]
    ) == ([], 0)

    blocked, _ = job._discover_postprocessor_plans(
        Conn(selector="report_207_migration", override="d105_2_ecodriving_migration"),
        [identity],
    )
    assert blocked[0].postprocessor_name == "unsupported_configured_postprocessor"
    assert blocked[0].error_category == "incompatible_report_selector"

    missing, _ = job._discover_postprocessor_plans(
        Conn(selector="report_207_migration", policy_found=False), [identity]
    )
    assert missing[0].error_category == "missing_report_policy"

    class PP:
        @staticmethod
        def run(client, run_id, params):
            assert params == {"client_code": "CLIENT", "raw_file_id": RAW_ID}
            return {"clients": [{"candidate_rows": 1, "migrated_rows": 1, "status": "OK"}]}
    original = registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR]
    try:
        registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR] = replace(original, runner=PP.run)
        result = job._execute_postprocessor_plan(Client(), "parent", plans[0])
    finally:
        registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR] = original
    assert result.outcome == job.WorkflowBPostprocessorOutcome.SUCCEEDED and result.records_affected == 1


def test_invalid_and_parent_unsupported_are_typed_failures() -> None:
    identity = s3_loaded().successful_load_identities[0]
    d105_identity = type(identity)(
        identity.raw_file_id,
        identity.client_code,
        "report_d105_2_ecodriving",
        identity.destination_schema,
        "report_d105_2_ecodriving",
        identity.source_cleaned_artifact_id,
        identity.final_status,
    )
    for conn, selected_identity, category in (
        (
            Conn(selector="report_207_migration", override="d105_2_ecodriving_migration"),
            identity,
            "incompatible_report_selector",
        ),
        (
            Conn(selector="api_migration", override="d105_2_ecodriving_migration"),
            d105_identity,
            "parent_unsupported_selector",
        ),
    ):
        plans, _ = job._discover_postprocessor_plans(conn, [selected_identity])
        result = job.WorkflowBBatchResult(lock_acquired=True)
        result.stage3.result = s3_loaded()
        result.stage3.started = result.stage3.completed = True
        result.postprocessors.append(job._execute_postprocessor_plan(Client(), "parent", plans[0]))
        assert result.postprocessors[0].error_category == category
        try:
            job._finish_or_raise(result)
        except job.WorkflowBOrchestrationError as exc:
            assert exc.partial_result.non_retryable_failure_count == 1
        else:
            raise AssertionError("unsupported selector did not fail closed")



def test_postprocessor_schema_readiness_is_non_retryable_operator_action() -> None:
    plan = job.WorkflowBPostprocessorPlan(
        job.REPORT_207_POSTPROCESSOR,
        RAW_ID,
        "CLIENT",
        "report_207",
        "artifact",
        "report_207_migration",
    )

    class PP:
        @staticmethod
        def run(*_args, **_kwargs):
            raise Stage3SchemaReadinessError([
                MissingSchemaObject(
                    "telematics_reports", "report_207", "unique index", "report_207__record_id_uidx"
                )
            ])

    original = registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR]
    try:
        registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR] = replace(original, runner=PP.run)
        result = job._execute_postprocessor_plan(Client(), "parent", plan)
    finally:
        registry.POSTPROCESSOR_REGISTRY[job.REPORT_207_POSTPROCESSOR] = original

    assert result.outcome == job.WorkflowBPostprocessorOutcome.FAILED_SCHEMA_NOT_READY
    assert result.retryable is False
    assert result.operator_action_required is True
    assert result.error_category == "schema_not_ready"
    assert "report_207__record_id_uidx" in result.error_detail
    print("PASS: postprocessor schema readiness propagates as non-retryable operator action")



def test_alpha_dependency_registry_and_failure_isolation() -> None:
    identity = Stage3ItemResult(
        RAW_ID, "ALPHA00001", "Alpha_GPS_Baza_LOG", Stage3Outcome.LOADED,
        destination_schema="telematics_reports", destination_table="Alpha_GPS_Baza_LOG",
        persisted_status="OK", source_cleaned_artifact_id="cleaned-artifact",
    )
    stage3 = Stage3BatchResult(discovered_candidate_count=1, eligible_count=1, items=[identity])
    successful = stage3.successful_load_identities[0]
    plans, duplicates = job._discover_postprocessor_plans(Conn(selector="disabled"), [successful])
    assert duplicates == 0 and len(plans) == 1
    plan = plans[0]
    assert plan.postprocessor_name == registry.ALPHA_DYSPONENT_POSTPROCESSOR
    assert plan.execution_mode is registry.PostprocessorExecutionMode.EXECUTE
    assert plan.parameters["dry_run"] is False and plan.parameters["process_all"] is True
    assert plan.parameters["date_from"] == "2026-07-21"
    assert plan.parameters["date_to"] == "2026-07-23"
    assert plan.parameters["source_raw_file_id"] == RAW_ID

    assert registry.resolve_postprocessor(
        registry.ALPHA_DYSPONENT_POSTPROCESSOR, client_code="ALPHA00001",
        report_type="Alpha_GPS_Baza_LOG", destination_schema="telematics_reports",
        destination_table="Alpha_GPS_Baza_LOG",
    ).dependency_coupled
    assert registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR].supported_modes == frozenset({
        registry.PostprocessorExecutionMode.EXECUTE,
        registry.PostprocessorExecutionMode.DRY_RUN,
    })
    assert registry.POSTPROCESSOR_REGISTRY[registry.REPORT_207_POSTPROCESSOR].supported_modes == frozenset({
        registry.PostprocessorExecutionMode.EXECUTE,
    })
    try:
        registry.resolve_postprocessor(
            registry.REPORT_207_POSTPROCESSOR, client_code="CLIENT",
            report_type="report_207", destination_schema="telematics_reports",
            destination_table="report_207",
            execution_mode=registry.PostprocessorExecutionMode.DRY_RUN,
        )
    except registry.UnsupportedPostprocessorConfiguration:
        pass
    else:
        raise AssertionError("unsupported Report 207 dry-run was accepted")
    for client_code, report_type in (("BRAVO00016", "Alpha_GPS_Baza_LOG"), ("ALPHA00001", "report_207")):
        try:
            registry.resolve_postprocessor(
                registry.ALPHA_DYSPONENT_POSTPROCESSOR, client_code=client_code,
                report_type=report_type, destination_schema="telematics_reports",
                destination_table="Alpha_GPS_Baza_LOG",
            )
        except registry.UnsupportedPostprocessorConfiguration:
            pass
        else:
            raise AssertionError("ALPHA postprocessor applicability escaped its static scope")
    try:
        registry.resolve_postprocessor("unknown", client_code="ALPHA00001", report_type="Alpha_GPS_Baza_LOG")
    except registry.UnsupportedPostprocessorConfiguration:
        pass
    else:
        raise AssertionError("unknown postprocessor was accepted")

    class FailedEnrichment:
        @staticmethod
        def run(*_args, **_kwargs):
            raise job.EnrichmentPreconditionError(job.COVERAGE_BELOW_THRESHOLD, "low")

    original = registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR]
    try:
        registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR] = replace(
            original, runner=FailedEnrichment.run
        )
        result = job.WorkflowBBatchResult(lock_acquired=True)
        result.stage3.started = result.stage3.completed = True
        result.stage3.result = stage3
        result.postprocessors.append(job._execute_postprocessor_plan(Client(), "parent", plan))
        assert result.stage3.result.successful_load_identities
        assert result.postprocessors[0].outcome == job.WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE
        try:
            job._finish_or_raise(result)
        except job.WorkflowBOrchestrationError as exc:
            assert exc.partial_result.stage3.completed
        else:
            raise AssertionError("failed enrichment did not propagate")
    finally:
        registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR] = original


def test_params_review_and_serialization() -> None:
    job._validate_params({})
    for params in (
        {"stage2": {"force_reprocess": True}}, {"stage2": {"input_files": ["x"]}},
        {"stage3": {"force_reprocess": True}}, {"stage3": {"dry_run": True}},
        {"stage3": {"raw_file_id": RAW_ID}}, {"postprocessors": {"selector": "x"}},
        {"postprocessor_execution_mode": "dry_run"},
    ):
        try: job._validate_params(params)
        except ValueError: pass
        else: raise AssertionError(f"unsafe params accepted: {params}")

    # A *replayable* review item. After review disproved the Stage 2 position
    # argument — targeted reprocessing re-stamps `stage2_updated_at` and moves a
    # swept row behind the sweep backstop — Stage 3 is the only class left whose
    # rediscovery the repository still guarantees: discovery runs with no LIMIT,
    # so every row in an eligible durable status comes back on every cycle. The
    # stubbed incident store having written nothing therefore loses no evidence.
    review = Stage3BatchResult(items=[Stage3ItemResult(
        raw_file_id=RAW_ID, client_code="BRAVO00016", report_type="report_207",
        outcome=Stage3Outcome.BLOCKED_OPERATOR_ACTION, persisted_status=None,
        error_category="missing_report_policy", operator_action_required=True)])
    with Patch(_platform_pg_conn=lambda: Conn(), fetch_reports_batch=lambda *_a, **_k: s1_zero(),
               process_stage2_batch=lambda *_a, **_k: Stage2BatchResult(),
               process_stage3_batch=lambda *_a, **_k: review):
        result = job.run_workflow_b_batch(Client(), "parent", {})
    assert result.outcome == job.WorkflowBOutcome.SUCCEEDED_WITH_REVIEW_ITEMS
    assert result.unresolved_signal_safety_failure is False
    payload = json.dumps(result.to_dict()).lower()
    for word in ("sender", "recipient", "subject", "filename", "password", "idempotency_key", "sql_parameters"):
        assert word not in payload

    # The same cycle with a *one-shot* review item. Every Stage 2 unresolved
    # condition is now in that class, sweep-observed ones included, so a cycle
    # that observed one and wrote nothing durable for it cannot settle as a
    # review outcome. This is the only signalling condition allowed to change a
    # verdict.
    one_shot = Stage2BatchResult(items=[Stage2ItemResult(
        RAW_ID, Stage2Outcome.STRANDED_UNROUTABLE, persisted_status="OK",
        reason_code="missing_client_code", review_required=True)])
    with Patch(_platform_pg_conn=lambda: Conn(), fetch_reports_batch=lambda *_a, **_k: s1_zero(),
               process_stage2_batch=lambda *_a, **_k: one_shot, process_stage3_batch=lambda *_a, **_k: s3_zero()):
        try:
            job.run_workflow_b_batch(Client(), "parent", {})
        except job.WorkflowBOrchestrationError as exc:
            assert exc.partial_result.outcome == job.WorkflowBOutcome.FAILED_NON_RETRYABLE
            assert exc.partial_result.unresolved_signal_safety_failure is True
            assert "one_shot_unresolved_input_without_durable_signal" in str(exc)
        else:
            raise AssertionError("an unrepresented one-shot input settled as a success")


def test_alpha_manual_source_refresh_boundaries() -> None:
    identity = Stage3ItemResult(
        RAW_ID, "ALPHA00001", "Alpha_GPS_Baza_LOG", Stage3Outcome.LOADED,
        destination_schema="telematics_reports", destination_table="Alpha_GPS_Baza_LOG",
        persisted_status="OK", source_cleaned_artifact_id="cleaned-artifact",
    )
    committed = Stage3BatchResult(discovered_candidate_count=1, eligible_count=1, items=[identity])
    pending = job.AlphaSourceSelection(
        type(committed.successful_load_identities[0])(
            RAW_ID, "ALPHA00001", "Alpha_GPS_Baza_LOG", "telematics_reports",
            "Alpha_GPS_Baza_LOG", "cleaned-artifact", "PENDING",
        ), True, True,
    )
    events = []
    stage1 = Stage1BatchResult(
        mailbox_check_completed=True,
        items=[Stage1ItemResult(Stage1Outcome.CREATED, raw_file_id=RAW_ID)],
    )
    stage2 = Stage2BatchResult(items=[
        Stage2ItemResult(
            RAW_ID, Stage2Outcome.SUCCEEDED_CREATED, persisted_status="OK",
            client_code="ALPHA00001", report_type="Alpha_GPS_Baza_LOG",
            artifact_id="cleaned-artifact",
        )
    ])

    class DryRunEnrichment:
        @staticmethod
        def run(_client, _run_id, params):
            events.append("postprocessor")
            assert params["dry_run"] is True
            assert params["source_raw_file_id"] == RAW_ID
            assert params["source_cleaned_artifact_id"] == "cleaned-artifact"
            return {
                "readiness_passed": True, "status": "OK", "rows_updated": 0,
                "target_trips_in_scope": 10, "planned_updates": 8,
                "source_rows_inspected": 20, "source_loaded_at": "2026-07-27T10:00:00+00:00",
                "source_business_date_max": "2026-07-26",
                "predicted_trip_coverage_percent": "99.00",
                "predicted_distance_coverage_percent": "99.50",
            }

    original = registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR]
    try:
        registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR] = replace(
            original, runner=DryRunEnrichment.run
        )
        with Patch(
            _platform_pg_conn=lambda: Conn(selector="disabled"),
            fetch_reports_batch=lambda *_a, **_k: events.append("stage1") or stage1,
            process_stage2_batch=lambda *_a, **_k: events.append("stage2") or stage2,
            process_stage3_batch=lambda *_a, **_k: events.append("stage3_commit") or committed,
            _alpha_stage1_raw_file_ids=lambda *_a, **_k: [RAW_ID],
            _select_alpha_source_for_refresh=lambda *_a, **_k: pending,
        ):
            result = job.run_alpha_source_refresh_batch(Client(), "manual-refresh")
    finally:
        registry.POSTPROCESSOR_REGISTRY[registry.ALPHA_DYSPONENT_POSTPROCESSOR] = original
    assert events == ["stage1", "stage2", "stage3_commit", "postprocessor"]
    assert result.source_load_committed and result.target_rows_modified == 0
    assert result.postprocessor.execution_mode is registry.PostprocessorExecutionMode.DRY_RUN
    assert result.outcome == job.AlphaSourceRefreshOutcome.SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_PASSED
    assert result.normal_execute_postprocessor_outstanding
    assert result.to_dict()["source_row_count"] == 20

    failed_postprocessor = job.WorkflowBPostprocessorResult(
        registry.ALPHA_DYSPONENT_POSTPROCESSOR, RAW_ID, "ALPHA00001",
        "Alpha_GPS_Baza_LOG", job.WorkflowBPostprocessorOutcome.FAILED_RETRYABLE,
        retryable=True, execution_mode=registry.PostprocessorExecutionMode.DRY_RUN,
    )
    with Patch(
        _platform_pg_conn=lambda: Conn(selector="disabled"),
        fetch_reports_batch=lambda *_a, **_k: stage1,
        process_stage2_batch=lambda *_a, **_k: stage2,
        process_stage3_batch=lambda *_a, **_k: committed,
        _alpha_stage1_raw_file_ids=lambda *_a, **_k: [RAW_ID],
        _select_alpha_source_for_refresh=lambda *_a, **_k: pending,
        _execute_postprocessor_plan=lambda *_a, **_k: failed_postprocessor,
    ):
        partial = job.run_alpha_source_refresh_batch(Client(), "manual-refresh")
    assert partial.source_load_committed
    assert partial.outcome == job.AlphaSourceRefreshOutcome.SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_FAILED

    wrote = replace(failed_postprocessor, records_affected=1)
    with Patch(
        _platform_pg_conn=lambda: Conn(selector="disabled"),
        fetch_reports_batch=lambda *_a, **_k: stage1,
        process_stage2_batch=lambda *_a, **_k: stage2,
        process_stage3_batch=lambda *_a, **_k: committed,
        _alpha_stage1_raw_file_ids=lambda *_a, **_k: [RAW_ID],
        _select_alpha_source_for_refresh=lambda *_a, **_k: pending,
        _execute_postprocessor_plan=lambda *_a, **_k: wrote,
    ):
        blocked = job.run_alpha_source_refresh_batch(Client(), "manual-refresh")
    assert blocked.outcome == job.AlphaSourceRefreshOutcome.BLOCKED_TARGET_WRITE_DETECTED


def test_alpha_manual_source_refresh_idempotent_outcomes() -> None:
    committed_identity = Stage3ItemResult(
        RAW_ID, "ALPHA00001", "Alpha_GPS_Baza_LOG", Stage3Outcome.LOADED,
        destination_schema="telematics_reports", destination_table="Alpha_GPS_Baza_LOG",
        persisted_status="OK", source_cleaned_artifact_id="cleaned-artifact",
    )
    existing = job.AlphaSourceSelection(
        Stage3BatchResult(items=[committed_identity]).successful_load_identities[0],
        False, False,
    )
    passed = job.WorkflowBPostprocessorResult(
        registry.ALPHA_DYSPONENT_POSTPROCESSOR, RAW_ID, "ALPHA00001",
        "Alpha_GPS_Baza_LOG", job.WorkflowBPostprocessorOutcome.SUCCEEDED,
        execution_mode=registry.PostprocessorExecutionMode.DRY_RUN,
        details={"readiness_passed": True, "rows_updated": 0},
    )
    with Patch(
        _platform_pg_conn=lambda: Conn(selector="disabled"),
        fetch_reports_batch=lambda *_a, **_k: s1_zero(),
        process_stage2_batch=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
        process_stage3_batch=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
        _alpha_stage1_raw_file_ids=lambda *_a, **_k: [],
        _select_alpha_source_for_refresh=lambda *_a, **_k: existing,
        _execute_postprocessor_plan=lambda *_a, **_k: passed,
    ):
        already = job.run_alpha_source_refresh_batch(Client(), "manual-refresh")
    assert already.outcome == job.AlphaSourceRefreshOutcome.ALREADY_LOADED
    assert not already.source_load_committed and already.target_rows_modified == 0

    with Patch(
        _platform_pg_conn=lambda: Conn(selector="disabled"),
        fetch_reports_batch=lambda *_a, **_k: s1_zero(),
        _alpha_stage1_raw_file_ids=lambda *_a, **_k: [],
        _select_alpha_source_for_refresh=lambda *_a, **_k: None,
    ):
        none = job.run_alpha_source_refresh_batch(Client(), "manual-refresh")
    assert none.outcome == job.AlphaSourceRefreshOutcome.NO_NEWER_SOURCE_AVAILABLE
    assert none.postprocessor is None


def test_alpha_stage2_input_is_bounded_by_static_report_key() -> None:
    second = "278ec6a3-6068-4090-8295-e62c0c6f6224"
    stage1 = Stage1BatchResult(items=[
        Stage1ItemResult(Stage1Outcome.CREATED, raw_file_id=RAW_ID),
        Stage1ItemResult(Stage1Outcome.CREATED, raw_file_id=second),
        Stage1ItemResult(Stage1Outcome.REUSED_MESSAGE, raw_file_id="ignored"),
    ])

    class FilterCursor:
        def __init__(self): self.params = None; self.query = ""
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def execute(self, query, params): self.query = str(query); self.params = params
        def fetchall(self): return [{"raw_file_id": RAW_ID}]
    class FilterConn:
        def __init__(self): self.cur = FilterCursor(); self.rollbacks = 0
        def cursor(self): return self.cur
        def rollback(self): self.rollbacks += 1
    conn = FilterConn()
    selected = job._alpha_stage1_raw_file_ids(conn, stage1)
    assert selected == [RAW_ID]
    assert conn.cur.params == ([RAW_ID, second], job.ALPHA_SOURCE_REPORT_KEY)
    assert "report_key = %s" in conn.cur.query and "status = 'NORMALIZED'" in conn.cur.query
    assert conn.rollbacks == 1


def main() -> None:
    test_lock_and_healthy_sequence()
    test_typed_continuation_and_unexpected_stop()
    test_stage2_and_stage3_partial_continuation()
    test_postprocessor_selection_dedup_and_execution()
    test_invalid_and_parent_unsupported_are_typed_failures()
    test_postprocessor_schema_readiness_is_non_retryable_operator_action()
    test_alpha_dependency_registry_and_failure_isolation()
    test_params_review_and_serialization()
    test_alpha_manual_source_refresh_boundaries()
    test_alpha_manual_source_refresh_idempotent_outcomes()
    test_alpha_stage2_input_is_bounded_by_static_report_key()
    print("OK - Workflow B parent orchestrator regressions passed")


if __name__ == "__main__": main()
