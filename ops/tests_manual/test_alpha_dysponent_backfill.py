#!/usr/bin/env python3
from __future__ import annotations
import io
from contextlib import redirect_stderr, redirect_stdout
from jobs.common.environment_identity import EnvironmentIdentityError
from ops import backfill_alpha00001_dysponent_id as cli


def invoke(argv, *, ready=True):
    calls = []
    originals = cli._load_dotenv, cli._resolve_client, cli.enrichment.run
    try:
        cli._load_dotenv = lambda: None
        cli._resolve_client = lambda args: (calls.append(("identity", args.client_code, args.client_id)) or ("ALPHA00001", "client-1"))
        def run(_client, _run_id, params):
            calls.append(("run", params))
            return {"client_id": "client-1", "readiness_passed": ready,
                    "resolved_start_date": "2026-07-21", "resolved_end_date_exclusive": "2026-07-23", "rows_updated": 0}
        cli.enrichment.run = run
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(argv)
    finally:
        cli._load_dotenv, cli._resolve_client, cli.enrichment.run = originals
    return code, calls


def test_default_is_dry_run_and_execute_is_explicit() -> None:
    code, calls = invoke(["--client-code", "ALPHA00001"])
    params = calls[-1][1]
    assert code == cli.EXIT_OK and calls[0][0] == "identity"
    assert params["dry_run"] is True and params["date_from"] == "2026-07-21"
    assert "date_to" not in params and params["overwrite_existing"] is False
    code, calls = invoke(["--client-id", "client-1", "--execute", "--overwrite-existing",
                          "--end-date", "2026-07-24", "--batch-size", "50"])
    params = calls[-1][1]
    assert code == cli.EXIT_OK and calls[0][0] == "identity"
    assert params["dry_run"] is False and params["overwrite_existing"] is True
    assert params["date_to"] == "2026-07-24" and params["batch_size"] == 50


def test_source_freshness_enforcement_is_opt_in() -> None:
    assert invoke(["--client-code", "ALPHA00001"])[1][-1][1]["require_fresh_source"] is False
    code, calls = invoke(["--client-code", "ALPHA00001", "--end-date", "2026-07-27",
                          "--require-fresh-source"])
    assert code == cli.EXIT_OK and calls[-1][1]["require_fresh_source"] is True


def test_readiness_and_validation_exit_codes() -> None:
    assert invoke(["--client-code", "ALPHA00001"], ready=False)[0] == cli.EXIT_NOT_READY
    assert invoke(["--client-code", "ALPHA00001", "--max-ambiguities", "-1"])[0] == cli.EXIT_INVALID_PARAMETERS


def test_identity_failure_prevents_job_call() -> None:
    originals = cli._load_dotenv, cli._resolve_client, cli.enrichment.run
    called = []
    try:
        cli._load_dotenv = lambda: None
        cli._resolve_client = lambda _args: (_ for _ in ()).throw(EnvironmentIdentityError("TEST", "not verified"))
        cli.enrichment.run = lambda *_a, **_k: called.append(True)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["--client-code", "ALPHA00001", "--execute"])
    finally:
        cli._load_dotenv, cli._resolve_client, cli.enrichment.run = originals
    assert code == cli.EXIT_IDENTITY_NOT_VERIFIED and called == []


def main() -> None:
    test_default_is_dry_run_and_execute_is_explicit()
    test_source_freshness_enforcement_is_opt_in()
    test_readiness_and_validation_exit_codes()
    test_identity_failure_prevents_job_call()
    print("OK - ALPHA Dysponent backfill CLI regressions passed")


if __name__ == "__main__": main()
