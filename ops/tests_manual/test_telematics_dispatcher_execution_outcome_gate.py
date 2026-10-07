#!/usr/bin/env python3
"""M3 — the scheduled dispatcher's coverage-advance gate.

WHAT THIS PINS.
    `rc == 0` is necessary and never sufficient. A compatibility `trips_sync`
    fire may advance `covered_through_ts` only when the child left a terminal
    execution record that is present, parses, verifies against *this* claim
    (including platform-run identity) and is coverage-eligible. Everything else
    finalizes the fire FAILED and leaves the watermark exactly where it was.

WHY IT IS WRITTEN AT THIS LEVEL.
    The properties under test are decisions `run_prepared` makes between the
    child exiting and `_finalize_compat_success` being called. They need no
    database: the finalizer is observed through a stand-in that records whether
    it was invoked at all, which is the sharpest possible statement of "coverage
    did not advance" — not "advanced by zero", but "never reached". The
    finalizer's own CAS and monotonicity behaviour has its own Postgres suite
    (`test_telematics_coverage_finalization_postgres.py`) and is untouched here.

    The disabled-schedule race is reproduced by its semantic essence rather than
    by real timing: a child that returns 0 having skipped. That is exactly what
    the race produces, and it is deterministic.
"""
from __future__ import annotations

import dataclasses
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.tests_manual import telematics_execution_outcome_fixtures as eo_fixtures  # noqa: E402
from jobs.api.telematics import dispatcher as disp  # noqa: E402
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED,
    CoverageGateResult,
)
from jobs.api.telematics.execution_outcome import ExecutionOutcome  # noqa: E402

CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_CODE = "TEST00001"
SCHEDULE_ID = "b454f82c-5857-4bab-8342-b7258e5cf7de"
RUN_HISTORY_ID = "f6222a11-06ee-4e4f-8b25-302a9d963cfa"
DATASET = "trips_sync"

FIRE = datetime(2026, 8, 13, 2, 0, tzinfo=timezone.utc)
WINDOW_START = FIRE - timedelta(hours=76)
WINDOW_END = FIRE - timedelta(hours=3)

_failures: List[str] = []


def _check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


# --- doubles ---------------------------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeClient:
    def __init__(self) -> None:
        self.logs: List[Dict[str, Any]] = []

    def log(self, level, type_, source, message, run_id=None, context=None):
        self.logs.append(
            {"level": level, "message": message, "context": dict(context or {})}
        )

    def report_suspected_bug(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("no suspected bug is expected on these paths")


def _schedule() -> Any:
    return disp.ScheduleRow(
        schedule_id=SCHEDULE_ID,
        client_id=CLIENT_ID,
        client_code=CLIENT_CODE,
        client_name="Test Client",
        dataset_name=DATASET,
        job_module="jobs.api.telematics.sync_trips_and_speeding",
        frequency="daily",
        day_of_week=None,
        day_of_month=None,
        day_of_month_last=False,
        run_time=FIRE.time(),
        timezone_name="UTC",
        lookback_days=3,
        overwrite_existing=True,
        enabled=True,
        event_enrichment_mode="disabled",
        trips_pagination_mode="data_invariants_v1",
        trips_stabilization_delay_seconds=10800,
        trips_overlap_seconds=3600,
        trips_max_recovery_span_seconds=2678400,
    )


def _prepared() -> disp.PreparedDispatcherRun:
    return disp.PreparedDispatcherRun(
        conn=_FakeConn(),
        schedule=_schedule(),
        fire_utc=FIRE,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        run_history_id=RUN_HISTORY_ID,
        stale_rows=[],
        stale_after_minutes=720,
        nominal_window_start=FIRE - timedelta(days=3),
        nominal_window_end=FIRE,
        coverage_state=object(),
        gate_result=CoverageGateResult(
            allowed=True,
            classification=COVERAGE_GATE_ALLOWED,
            abort_code=None,
            reason="test",
            effective_window=None,
            requires_gap_persistence=False,
            coverage_start_ts=None,
            covered_through_ts=None,
            bootstrap_status="READY",
        ),
    )


def _job_params() -> Dict[str, Any]:
    """Exactly what `_build_job_params` produces for this compatibility fire."""
    return {
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "trigger": "SCHEDULED",
        "window_start_ts": disp._iso_z(WINDOW_START),
        "window_end_ts": disp._iso_z(WINDOW_END),
    }


def _record(builder, *, platform_run_id: Optional[str], **kwargs) -> ExecutionOutcome:
    return ExecutionOutcome.from_mapping(
        builder(
            _job_params(),
            schedule_id=SCHEDULE_ID,
            platform_run_id=platform_run_id,
            **kwargs,
        )
    )


def _run_tick(
    *,
    rc: int,
    collected: Optional[disp.ScheduledExecutionOutcome],
    platform_run_id: Optional[str],
) -> Dict[str, Any]:
    """One `run_prepared` pass with the subprocess and finalizer replaced."""
    observed: Dict[str, Any] = {
        "finalize_compat_success_called": False,
        "finalized": [],
        "error": None,
    }
    prepared = _prepared()
    client = _FakeClient()

    def _fake_launch(**kwargs):
        assert kwargs["collect_execution_outcome"] is True, (
            "a compatibility trips fire must always request a terminal record"
        )
        if platform_run_id and kwargs.get("on_platform_run_id"):
            kwargs["on_platform_run_id"](platform_run_id)
        return rc, "", "boom" if rc else "", platform_run_id, collected

    def _fake_finalize_compat_success(
        conn, *, prepared, completeness=None, platform_run_id=None,
    ):
        observed["finalize_compat_success_called"] = True
        # M4: the finalizer is never reached without a verified proof and the
        # platform-run identity to stamp it with. Recorded rather than ignored,
        # so "coverage finalization ran" cannot quietly become "ran with
        # nothing to make durable".
        observed["finalize_completeness"] = completeness
        observed["finalize_platform_run_id"] = platform_run_id
        return True

    saved = {
        name: getattr(disp, name)
        for name in (
            "_launch_job", "_finalize_compat_success", "_finalize_run",
            "_set_platform_run_id", "_release_dispatcher_lock",
        )
    }
    disp._launch_job = _fake_launch
    disp._finalize_compat_success = _fake_finalize_compat_success
    disp._finalize_run = (
        lambda conn, **kw: observed["finalized"].append(dict(kw))
    )
    disp._set_platform_run_id = lambda *a, **kw: None
    disp._release_dispatcher_lock = lambda value: None
    try:
        disp.run_prepared(client, "dispatcher-run", {}, prepared)
    except Exception as exc:  # noqa: BLE001
        observed["error"] = exc
    finally:
        for name, value in saved.items():
            setattr(disp, name, value)
    observed["logs"] = client.logs
    return observed


def _assert_refused(label: str, result: Dict[str, Any], code: str) -> None:
    _check(
        f"{label}: coverage finalization is never reached",
        result["finalize_compat_success_called"] is False,
    )
    _check(
        f"{label}: the fire is finalized FAILED",
        len(result["finalized"]) == 1
        and result["finalized"][0]["status"] == "FAILED",
    )
    _check(
        f"{label}: the refusal reaches error_summary as {code}",
        code in str(result["finalized"][0].get("error") or ""),
    )
    _check(
        f"{label}: the tick still raises so run_context sees a failure",
        isinstance(result["error"], RuntimeError),
    )
    _check(
        f"{label}: no log claims success",
        not any("SUCCESS" in row["message"] for row in result["logs"]),
    )


# --- the properties --------------------------------------------------------


def test_eligible_committed_outcome_advances() -> None:
    pid = str(uuid.uuid4())
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(
                eo_fixtures.committed_outcome, platform_run_id=pid, upserted=18531
            ),
        ),
        platform_run_id=pid,
    )
    _check(
        "eligible committed outcome: coverage finalization runs",
        result["finalize_compat_success_called"] is True,
    )
    _check(
        "eligible committed outcome: no FAILED finalization",
        not any(f["status"] == "FAILED" for f in result["finalized"]),
    )
    _check(
        "eligible committed outcome: the tick does not raise",
        result["error"] is None,
    )
    _check(
        "eligible committed outcome: success is logged with the outcome",
        any(
            "SUCCESS" in row["message"]
            and row["context"].get("execution_outcome") == "EXECUTED_COMMITTED"
            for row in result["logs"]
        ),
    )


def test_eligible_zero_row_outcome_still_advances() -> None:
    """A genuine zero-row day must not stall the watermark (§6, §15)."""
    pid = str(uuid.uuid4())
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(
                eo_fixtures.committed_outcome, platform_run_id=pid, upserted=0
            ),
        ),
        platform_run_id=pid,
    )
    _check(
        "zero-row committed outcome: coverage finalization runs",
        result["finalize_compat_success_called"] is True,
    )
    _check(
        "zero-row committed outcome: the tick does not raise",
        result["error"] is None,
    )
    _check(
        "zero-row committed outcome: recorded as EXECUTED_ZERO_ROWS_COMMITTED",
        any(
            row["context"].get("execution_outcome")
            == "EXECUTED_ZERO_ROWS_COMMITTED"
            for row in result["logs"]
        ),
    )


def test_skipped_disabled_schedule_outcome_refuses() -> None:
    """The confirmed false-success path: rc=0, no work, no advancement."""
    pid = str(uuid.uuid4())
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(
                eo_fixtures.skipped_disabled_schedule_outcome,
                platform_run_id=pid,
            ),
        ),
        platform_run_id=pid,
    )
    _assert_refused(
        "disabled-schedule skip", result, disp.TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE
    )


def test_missing_outcome_refuses() -> None:
    from jobs.api.telematics.execution_outcome import ExecutionOutcomeError

    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            error=ExecutionOutcomeError(
                "EXECUTION_OUTCOME_ABSENT",
                "the business process wrote no terminal execution record",
            ),
        ),
        platform_run_id=str(uuid.uuid4()),
    )
    _assert_refused("absent record", result, "EXECUTION_OUTCOME_ABSENT")


def test_identity_mismatch_refuses() -> None:
    """A well-formed record bound to a different platform run must not pass."""
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(
                eo_fixtures.committed_outcome,
                platform_run_id=str(uuid.uuid4()),
            ),
        ),
        platform_run_id=str(uuid.uuid4()),
    )
    _assert_refused(
        "platform-run identity mismatch", result,
        "EXECUTION_OUTCOME_IDENTITY_MISMATCH",
    )


def test_missing_platform_run_identity_refuses() -> None:
    """An eligible record with no platform run id proves nothing about a run."""
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(eo_fixtures.committed_outcome, platform_run_id=None),
        ),
        platform_run_id=None,
    )
    _assert_refused(
        "absent platform-run identity", result,
        "EXECUTION_OUTCOME_PLATFORM_RUN_ID_MISSING",
    )


def test_recovery_record_refused_for_a_scheduled_claim() -> None:
    """A manual-recovery record replayed against a scheduled fire is not proof."""
    pid = str(uuid.uuid4())
    params = _job_params() | {"manual_recovery_run_id": str(uuid.uuid4())}
    outcome = ExecutionOutcome.from_mapping(
        eo_fixtures.committed_outcome(
            params, schedule_id=SCHEDULE_ID, platform_run_id=pid
        )
    )
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(requested=True, outcome=outcome),
        platform_run_id=pid,
    )
    _assert_refused(
        "recovery record on a scheduled claim", result,
        "EXECUTION_OUTCOME_IDENTITY_MISMATCH",
    )


def test_window_mismatch_refuses() -> None:
    """A record for a different window cannot certify this window."""
    pid = str(uuid.uuid4())
    params = _job_params() | {
        "window_start_ts": disp._iso_z(WINDOW_START - timedelta(days=1)),
    }
    outcome = ExecutionOutcome.from_mapping(
        eo_fixtures.committed_outcome(
            params, schedule_id=SCHEDULE_ID, platform_run_id=pid
        )
    )
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(requested=True, outcome=outcome),
        platform_run_id=pid,
    )
    _assert_refused(
        "requested-window mismatch", result,
        "EXECUTION_OUTCOME_IDENTITY_MISMATCH",
    )


def test_non_zero_returncode_still_fails_without_reaching_the_gate() -> None:
    """rc != 0 keeps its existing semantics: FAILED, no advancement."""
    pid = str(uuid.uuid4())
    result = _run_tick(
        rc=3,
        collected=disp.ScheduledExecutionOutcome(
            requested=True,
            outcome=_record(eo_fixtures.committed_outcome, platform_run_id=pid),
        ),
        platform_run_id=pid,
    )
    _check(
        "rc != 0: coverage finalization is never reached",
        result["finalize_compat_success_called"] is False,
    )
    _check(
        "rc != 0: finalized FAILED",
        len(result["finalized"]) == 1
        and result["finalized"][0]["status"] == "FAILED",
    )
    _check(
        "rc != 0: an eligible record does not rescue a failed process",
        isinstance(result["error"], RuntimeError),
    )


def test_absent_client_code_does_not_become_a_positive_assertion() -> None:
    """`ScheduleRow.client_code` is `COALESCE(..., '')`; the record carries None.

    Passing `''` straight through would assert an identity the child can never
    satisfy, refusing every fire for that client forever until the coverage gate
    escalated to a durable `GAP_DETECTED` needing manual recovery. No production
    client has a NULL code today, so this pins a latent trap, not a live one.
    """
    pid = str(uuid.uuid4())
    params = _job_params()
    params.pop("client_code")
    outcome = ExecutionOutcome.from_mapping(
        eo_fixtures.committed_outcome(
            params, schedule_id=SCHEDULE_ID, platform_run_id=pid
        )
    )
    _check(
        "codeless client: the fixture really carries no client_code",
        outcome.client_code is None,
    )

    observed: Dict[str, Any] = {"called": False}

    def _finalize(conn, *, prepared):
        observed["called"] = True
        return True

    prepared = _prepared()
    prepared.schedule = dataclasses.replace(prepared.schedule, client_code="")
    saved = disp._finalize_compat_success
    disp._finalize_compat_success = _finalize
    try:
        disp._require_coverage_eligible_outcome(
            prepared=prepared,
            schedule=prepared.schedule,
            collected=disp.ScheduledExecutionOutcome(
                requested=True, outcome=outcome
            ),
            platform_run_id=pid,
        )
        accepted = True
    except disp.ScheduledOutcomeRefused as exc:
        accepted = False
        print(f"      refused: {exc}")
    finally:
        disp._finalize_compat_success = saved
    _check(
        "codeless client: an empty client_code compares as 'nothing to compare'",
        accepted,
    )


def test_a_gate_defect_still_finalizes_the_fire() -> None:
    """An unexpected exception must cost one fire, not the whole workflow.

    Escaping the handler would skip `_finalize_run`, leave the history row
    RUNNING, and `_count_running() > 0` then makes every later tick a no-op for
    every client until the 12-hour stale sweep.
    """
    saved = disp._require_coverage_eligible_outcome
    disp._require_coverage_eligible_outcome = (
        lambda **kwargs: (_ for _ in ()).throw(TypeError("gate defect"))
    )
    try:
        result = _run_tick(
            rc=0,
            collected=disp.ScheduledExecutionOutcome(requested=True),
            platform_run_id=str(uuid.uuid4()),
        )
    finally:
        disp._require_coverage_eligible_outcome = saved
    _assert_refused("gate defect", result, disp.TRIPS_OUTCOME_GATE_FAILED)


def test_a_refusal_names_the_client_it_stalled() -> None:
    """The terminal-failure incident reads the tick's params, which name nobody."""
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(requested=False),
        platform_run_id=str(uuid.uuid4()),
    )
    message = str(result["error"])
    _check(
        "refusal message names the client and the schedule",
        CLIENT_CODE in message and SCHEDULE_ID in message,
    )


def test_a_long_refusal_reason_keeps_its_leading_code() -> None:
    """`_finalize_run` keeps the tail of error_summary, so bound the reason."""
    refusal = disp.ScheduledOutcomeRefused("SOME_CODE", "x" * 5000)
    _check(
        "an unbounded reason is truncated",
        len(refusal.reason) <= disp.OUTCOME_REFUSAL_REASON_MAX_CHARS,
    )
    _check(
        "the classification survives at the front of error_summary",
        str(refusal)[-4000:].startswith("SOME_CODE: "),
    )


def test_not_collected_refuses() -> None:
    """Fail closed if a future edit forgets to request the record."""
    result = _run_tick(
        rc=0,
        collected=disp.ScheduledExecutionOutcome(requested=False),
        platform_run_id=str(uuid.uuid4()),
    )
    _assert_refused(
        "record never requested", result, disp.TRIPS_OUTCOME_NOT_COLLECTED
    )


def main() -> int:
    print("# M3 — dispatcher coverage-advance outcome gate\n")
    for test in (
        test_eligible_committed_outcome_advances,
        test_eligible_zero_row_outcome_still_advances,
        test_skipped_disabled_schedule_outcome_refuses,
        test_missing_outcome_refuses,
        test_identity_mismatch_refuses,
        test_missing_platform_run_identity_refuses,
        test_recovery_record_refused_for_a_scheduled_claim,
        test_window_mismatch_refuses,
        test_non_zero_returncode_still_fails_without_reaching_the_gate,
        test_absent_client_code_does_not_become_a_positive_assertion,
        test_a_gate_defect_still_finalizes_the_fire,
        test_a_refusal_names_the_client_it_stalled,
        test_a_long_refusal_reason_keeps_its_leading_code,
        test_not_collected_refuses,
    ):
        print(f"\n## {test.__name__}")
        test()
    print(
        f"\n{'FAILED' if _failures else 'ALL PASS'}"
        + (f" — {len(_failures)} assertion(s)" if _failures else "")
    )
    for failure in _failures:
        print(f"  - {failure}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
