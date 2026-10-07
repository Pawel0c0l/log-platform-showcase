#!/usr/bin/env python3
"""Shared fixtures for faking the one business subprocess of a recovery.

A stand-in for `ops.recover_telematics_trips_window.launch_sync` must now produce
the business job's structured terminal record as well as a return code, because
the coverage gate reads the record and treats the return code as necessary but
never sufficient. Building that record by hand in every suite would let the
suites drift from the real contract, so it is built here once, through the same
`ExecutionOutcome` type the production code parses.

This module is test support only. It is never imported by production code.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics.execution_outcome import (  # noqa: E402
    OUTCOME_EXECUTED_COMMITTED,
    OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED,
    OUTCOME_SKIPPED_DISABLED_SCHEDULE,
    SKIP_REASON_DISABLED_SCHEDULE,
    TRANSACTION_COMMITTED,
    TRANSACTION_NOT_ENTERED,
    ExecutionOutcome,
)
from jobs.api.telematics.request_evidence import (  # noqa: E402
    SUBWINDOW_COMPLETE,
    TERMINATION_SHORT_PAGE,
    TOTAL_RECONCILIATION_ABSENT,
    WINDOW_COMPLETENESS_VERSION,
    PageRequestRecord,
    SubWindowRecord,
    WindowCompleteness,
)


def complete_window(
    *,
    window_start_ts: datetime,
    window_end_ts: datetime,
    subwindows: int = 1,
    endpoint: str = "/trips",
    row_count: int = 3,
) -> WindowCompleteness:
    """A proof that `[start, end)` was exactly tiled and every unit completed.

    Built through the real types, not hand-written JSON, so a suite cannot drift
    from the contract the dispatcher actually parses. The tiling is an even
    split into `subwindows` contiguous half-open slices — the shape
    `_build_trip_fetch_chunks` produces — with the inclusive wire end one second
    short of each slice's exclusive end, exactly as the job does it.
    """
    start = window_start_ts.astimezone(timezone.utc)
    end = window_end_ts.astimezone(timezone.utc)
    span = (end - start) / subwindows
    records = []
    for index in range(1, subwindows + 1):
        covers_from = start + span * (index - 1)
        covers_to = end if index == subwindows else start + span * index
        requested_to = (
            covers_to if index == subwindows
            else covers_to - timedelta(seconds=1)
        )
        records.append(
            SubWindowRecord(
                index=index,
                covers_from_ts=covers_from,
                covers_to_ts=covers_to,
                requested_from_ts=covers_from,
                requested_to_ts=requested_to,
                sub_window_label=f"{covers_from.isoformat()}..{requested_to.isoformat()}",
                status=SUBWINDOW_COMPLETE,
                termination_reason=TERMINATION_SHORT_PAGE,
                total_reconciliation=TOTAL_RECONCILIATION_ABSENT,
                incomplete_reason=None,
                pages=(
                    PageRequestRecord(
                        request_id=str(uuid.uuid4()),
                        page=1,
                        wire_start_value=covers_from.strftime("%Y-%m-%d %H:%M:%S"),
                        wire_end_value=requested_to.strftime("%Y-%m-%d %H:%M:%S"),
                        request_started_at_utc=covers_to,
                        response_received_at_utc=covers_to,
                        http_status=200,
                        row_count=row_count,
                    ),
                ),
            )
        )
    return WindowCompleteness(
        version=WINDOW_COMPLETENESS_VERSION,
        endpoint=endpoint,
        effective_window_start_ts=start,
        effective_window_end_ts=end,
        subwindows=tuple(records),
    )


#: Distinguishes "caller did not mention completeness" from "caller explicitly
#: wants a record carrying none". `None` is a meaningful value here — it is the
#: absent-evidence case M4 must refuse — so it cannot double as the default.
_UNSET: Any = object()


def _parse(raw: object) -> datetime:
    text = str(raw)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _recovery_run_id(job_params: Dict[str, Any]) -> Optional[str]:
    """`None` for a scheduled fire, which carries no recovery identity.

    A scheduled dispatcher fire and a manual recovery produce records that
    differ in exactly this field, and the dispatcher's M3 gate asserts it is
    absent. Building both shapes from the same fixtures keeps that difference
    honest instead of letting each suite invent it.
    """
    raw = job_params.get("manual_recovery_run_id")
    return None if raw is None else str(raw)


def committed_outcome(
    job_params: Dict[str, Any],
    *,
    schedule_id: str,
    platform_run_id: Optional[str] = None,
    upserted: int = 3,
    prepared: Optional[int] = None,
    malformed: int = 0,
    completeness: Optional[WindowCompleteness] = _UNSET,
) -> Dict[str, Any]:
    """A genuine committed execution over exactly the requested window.

    `upserted=0` yields `EXECUTED_ZERO_ROWS_COMMITTED`, which is valid work and
    must advance coverage; anything above zero yields `EXECUTED_COMMITTED`.

    `completeness` defaults to a proof that exactly tiles the requested window,
    because that is what a healthy run produces and every M3 suite is about
    something else. Pass `None` to reproduce a record with no M4 evidence, or a
    hand-built carrier to reproduce a defective tiling.
    """
    window_start = _parse(job_params["window_start_ts"])
    window_end = _parse(job_params["window_end_ts"])
    if completeness is _UNSET:
        completeness = complete_window(
            window_start_ts=window_start,
            window_end_ts=window_end,
            row_count=upserted,
        )
    return ExecutionOutcome(
        outcome=(
            OUTCOME_EXECUTED_COMMITTED if upserted
            else OUTCOME_EXECUTED_ZERO_ROWS_COMMITTED
        ),
        client_id=str(job_params["client_id"]),
        client_code=job_params.get("client_code"),
        schedule_id=str(schedule_id),
        dataset_name="trips_sync",
        recovery_run_id=_recovery_run_id(job_params),
        platform_run_id=platform_run_id,
        requested_window_start_ts=window_start,
        requested_window_end_ts=window_end,
        provider_execution_entered=True,
        business_transaction_entered=True,
        transaction_status=TRANSACTION_COMMITTED,
        prepared_count=upserted if prepared is None else prepared,
        upserted_count=upserted,
        malformed_count=malformed,
        skipped=False,
        skip_reason=None,
        terminal_ts=datetime.now(timezone.utc).replace(microsecond=0),
        window_completeness=completeness,
    ).as_dict()


def recovery_job_summary(outcome: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The `job_summary` shape the recovery launcher persists on a recovery row.

    Activation reads the structured proof from here, so a fixture that seeds a
    historical row simply passes `None` to reproduce a pre-hardening row that
    carries no proof at all.
    """
    return {
        "job_module": "jobs.api.telematics.sync_trips_and_speeding",
        "trigger": "MANUAL_RECOVERY",
        "returncode": 0,
        "automatic_retries": 0,
        "execution_outcome": outcome,
        "execution_outcome_verified": outcome is not None,
        "coverage_advance_permitted": outcome is not None,
    }


def outcome_for_window(
    *,
    client_id: str,
    client_code: Optional[str],
    schedule_id: str,
    recovery_run_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    platform_run_id: Optional[str] = None,
    dataset_name: str = "trips_sync",
    upserted: int = 2,
) -> Dict[str, Any]:
    """A committed proof for a recovery row seeded directly by a fixture."""
    return committed_outcome(
        {
            "client_id": client_id,
            "client_code": client_code,
            "manual_recovery_run_id": recovery_run_id,
            "window_start_ts": window_start_ts.isoformat(),
            "window_end_ts": window_end_ts.isoformat(),
        },
        schedule_id=schedule_id,
        platform_run_id=platform_run_id,
        upserted=upserted,
    ) | {"dataset_name": dataset_name}


def skipped_disabled_schedule_outcome(
    job_params: Dict[str, Any],
    *,
    schedule_id: str,
    platform_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Exactly what the ECHO00001 cold start produced: rc=0 and no work."""
    return ExecutionOutcome(
        outcome=OUTCOME_SKIPPED_DISABLED_SCHEDULE,
        client_id=str(job_params["client_id"]),
        client_code=job_params.get("client_code"),
        schedule_id=str(schedule_id),
        dataset_name="trips_sync",
        recovery_run_id=_recovery_run_id(job_params),
        platform_run_id=platform_run_id,
        requested_window_start_ts=_parse(job_params["window_start_ts"]),
        requested_window_end_ts=_parse(job_params["window_end_ts"]),
        provider_execution_entered=False,
        business_transaction_entered=False,
        transaction_status=TRANSACTION_NOT_ENTERED,
        prepared_count=0,
        upserted_count=0,
        malformed_count=0,
        skipped=True,
        skip_reason=SKIP_REASON_DISABLED_SCHEDULE,
        terminal_ts=datetime.now(timezone.utc).replace(microsecond=0),
    ).as_dict()
