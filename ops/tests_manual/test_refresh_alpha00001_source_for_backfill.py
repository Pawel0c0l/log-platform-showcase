#!/usr/bin/env python3
"""Service-free safety tests for the dedicated ALPHA source-refresh command."""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from types import SimpleNamespace

from jobs.reports.workflow_b import orchestrator
from jobs.reports.workflow_b.postprocessor_registry import PostprocessorExecutionMode
from ops import refresh_alpha00001_source_for_backfill as tool


class Patch:
    def __init__(self, target, **values):
        self.target = target; self.values = values; self.originals = {}
    def __enter__(self):
        for name, value in self.values.items():
            self.originals[name] = getattr(self.target, name); setattr(self.target, name, value)
    def __exit__(self, *_args):
        for name, value in self.originals.items(): setattr(self.target, name, value)


def call(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = tool.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_plan_default_has_no_network_or_dml() -> None:
    runtime = SimpleNamespace(platform_identity_id=tool.EXPECTED_PLATFORM_ID)
    with Patch(tool, _load_dotenv=lambda: None, _execute_source_refresh=lambda *_a: (_ for _ in ()).throw(AssertionError())), \
         Patch(tool.environment_identity, load_runtime_identity=lambda: runtime):
        code, output, error = call([])
    assert code == tool.EXIT_OK and not error
    assert '"mode": "plan"' in output
    assert '"imap_connection": false' in output
    assert '"source_table_write": false' in output
    assert '"client_trips_write": false' in output


def test_execute_flag_attestation_and_exact_scope() -> None:
    with Patch(tool, _load_dotenv=lambda: None):
        code, _output, error = call(["--execute-source-refresh"])
    assert code == tool.EXIT_INVALID_INTENT and "exact --attestation" in error
    assert tool.EXPECTED_CLIENT_CODE == "ALPHA00001"
    assert tool.EXPECTED_REPORT == "Alpha_GPS_Baza_LOG"
    assert tool.EXPECTED_DESTINATION == "telematics_reports.Alpha_GPS_Baza_LOG"
    assert orchestrator.ALPHA_SOURCE_REPORT_KEY == "gps_baza_start_skrypt"
    assert "postprocessor=dry_run" in tool.EXACT_ATTESTATION
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            tool._parser().parse_args(["--client-code", "BRAVO00016"])
        except SystemExit:
            pass
        else:
            raise AssertionError("generic client selector was exposed")


def test_execute_dispatch_and_nonzero_write_failure() -> None:
    seen = []
    with Patch(
        tool,
        _load_dotenv=lambda: None,
        _execute_source_refresh=lambda attestation: seen.append(attestation) or {
            "outcome": "SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_PASSED",
            "target_rows_modified": 0,
        },
    ):
        code, output, error = call([
            "--execute-source-refresh", "--attestation", tool.EXACT_ATTESTATION,
        ])
    assert code == tool.EXIT_OK and not error and seen == [tool.EXACT_ATTESTATION]
    assert '"target_rows_modified": 0' in output

    result = orchestrator.AlphaSourceRefreshResult(
        outcome=orchestrator.AlphaSourceRefreshOutcome.BLOCKED_TARGET_WRITE_DETECTED,
        target_rows_modified=1,
        postprocessor=orchestrator.WorkflowBPostprocessorResult(
            "alpha00001_dysponent_id_enrichment", "raw", "ALPHA00001",
            "Alpha_GPS_Baza_LOG", orchestrator.WorkflowBPostprocessorOutcome.FAILED_NON_RETRYABLE,
            records_affected=1, execution_mode=PostprocessorExecutionMode.DRY_RUN,
        ),
    )
    with Patch(
        tool,
        _load_dotenv=lambda: None,
        _execute_source_refresh=lambda *_a: (_ for _ in ()).throw(tool.SourceRefreshOperationError(result)),
    ):
        code, _output, error = call([
            "--execute-source-refresh", "--attestation", tool.EXACT_ATTESTATION,
        ])
    assert code == tool.EXIT_BLOCKED and '"target_rows_modified": 1' in error


def test_runtime_checkout_and_identity_contract_is_fail_closed() -> None:
    source = Path(tool.__file__).read_text()
    for required in (
        "EXPECTED_PLATFORM_ID", "EXPECTED_CLIENT_ID", "EXPECTED_CLIENT_DB_ID",
        "attest_platform_identity", "attest_client_identity", "pg_stat_activity",
        "_try_global_lock", "runtime worktree must be clean",
        "runtime HEAD must equal the deployed origin/main ref",
    ):
        assert required in source
    with Patch(tool, _git=lambda *args: {
        ("branch", "--show-current"): "main",
        ("status", "--short", "--untracked-files=all"): "",
        ("rev-parse", "HEAD"): "same",
        ("rev-parse", "origin/main"): "same",
    }[args]), Patch(tool.socket, gethostname=lambda: tool.EXPECTED_HOST), \
         Patch(tool.getpass, getuser=lambda: tool.EXPECTED_USER):
        tool._require_runtime_checkout()


def test_conflicting_workflow_lock_is_typed_no_write() -> None:
    class Conn:
        def close(self): pass
    with Patch(
        orchestrator,
        _platform_pg_conn=lambda: Conn(),
        _try_global_lock=lambda _conn: False,
        fetch_reports_batch=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()),
    ):
        result = orchestrator.run_alpha_source_refresh_batch(object(), "run")
    assert result.outcome == orchestrator.AlphaSourceRefreshOutcome.SKIPPED_LOCKED
    assert not result.lock_acquired and result.target_rows_modified == 0


def main() -> None:
    test_plan_default_has_no_network_or_dml()
    test_execute_flag_attestation_and_exact_scope()
    test_execute_dispatch_and_nonzero_write_failure()
    test_runtime_checkout_and_identity_contract_is_fail_closed()
    test_conflicting_workflow_lock_is_typed_no_write()
    print("OK - dedicated ALPHA source-refresh operational safety checks passed")


if __name__ == "__main__": main()
