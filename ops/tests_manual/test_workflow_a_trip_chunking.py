#!/usr/bin/env python3
"""Manual tests for mandatory /trips job-level chunking.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_trip_chunking.py
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402
from jobs.api.telematics.provider_safety import TelematicsProviderSafetyError  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _chunks(start: str, end: str, chunk_days: int | None = None) -> list[job.TripFetchChunk]:
    return job._build_trip_fetch_chunks(
        window_start_ts=_ts(start),
        window_end_ts=_ts(end),
        chunk_days=job._trips_chunk_days({}) if chunk_days is None else chunk_days,
    )


def _assert_consecutive_non_overlapping(label: str, chunks: list[job.TripFetchChunk], chunk_days: int) -> None:
    max_delta = timedelta(days=chunk_days)
    ok = True
    details: list[str] = []
    for prev, curr in zip(chunks, chunks[1:]):
        if prev.exclusive_end_ts != curr.request_start_ts:
            ok = False
            details.append(f"gap/overlap: prev exclusive {prev.exclusive_end_ts}, next start {curr.request_start_ts}")
        if prev.request_end_ts >= curr.request_start_ts:
            ok = False
            details.append(f"provider overlap: prev request end {prev.request_end_ts}, next start {curr.request_start_ts}")
    for chunk in chunks:
        if chunk.exclusive_end_ts - chunk.request_start_ts > max_delta:
            ok = False
            details.append(f"chunk {chunk.index} too large: {chunk.exclusive_end_ts - chunk.request_start_ts}")
    _check(label, ok, "; ".join(details))


def test_chunk_boundaries() -> None:
    one_day = _chunks("2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z")
    _check("1-day input window produces 1 chunk", len(one_day) == 1, repr(one_day))
    _check("1-day final chunk preserves original end", one_day[0].request_end_ts == _ts("2026-04-02T00:00:00Z"))

    five_days_default = _chunks("2026-04-01T00:00:00Z", "2026-04-06T00:00:00Z")
    _check("5-day input with omitted chunk_days uses default 2-day chunks",
           len(five_days_default) == 3, repr(five_days_default))
    _assert_consecutive_non_overlapping(
        "5-day default chunks are consecutive, non-overlapping, max 2 days",
        five_days_default,
        2,
    )

    five_days_explicit = _chunks("2026-04-01T00:00:00Z", "2026-04-06T00:00:00Z", chunk_days=5)
    _check("explicit chunk_days=5 keeps 5-day input as 1 chunk", len(five_days_explicit) == 1, repr(five_days_explicit))
    _check("explicit chunk_days=5 final chunk preserves original end",
           five_days_explicit[0].request_end_ts == _ts("2026-04-06T00:00:00Z"))

    five_days_daily = _chunks("2026-04-01T00:00:00Z", "2026-04-06T00:00:00Z", chunk_days=1)
    _check("explicit chunk_days=1 creates daily chunks", len(five_days_daily) == 5, repr(five_days_daily))
    _assert_consecutive_non_overlapping(
        "daily chunks are consecutive, non-overlapping, max 1 day",
        five_days_daily,
        1,
    )

    six_days = _chunks("2026-04-01T00:00:00Z", "2026-04-07T00:00:00Z", chunk_days=5)
    _check("6-day input window produces 2 chunks", len(six_days) == 2, repr(six_days))
    _check(
        "6-day first provider chunk ends one second before next start",
        six_days[0].request_end_ts == _ts("2026-04-05T23:59:59Z")
        and six_days[0].exclusive_end_ts == _ts("2026-04-06T00:00:00Z")
        and six_days[1].request_start_ts == _ts("2026-04-06T00:00:00Z"),
        repr(six_days),
    )
    _assert_consecutive_non_overlapping("6-day chunks are consecutive, non-overlapping, max 5 days", six_days, 5)

    forty_five_days = _chunks("2026-04-01T00:00:00Z", "2026-05-16T00:00:00Z", chunk_days=5)
    _check("45-day input window produces 9 chunks with explicit chunk_days=5", len(forty_five_days) == 9)
    _assert_consecutive_non_overlapping(
        "45-day chunks are consecutive, non-overlapping, max 5 days",
        forty_five_days,
        5,
    )


def test_chunk_days_validation() -> None:
    _check("chunk_days omitted defaults to 2", job._trips_chunk_days({}) == 2)
    _check("dispatcher/default invocation path gets default chunking", len(_chunks(
        "2026-04-01T00:00:00Z", "2026-04-07T00:00:00Z",
        job._trips_chunk_days({"trigger": "DISPATCHER"}),
    )) == 3)
    _check("chunk_days=3 is accepted and used", len(_chunks(
        "2026-04-01T00:00:00Z", "2026-04-07T00:00:00Z",
        job._trips_chunk_days({"chunk_days": 3}),
    )) == 2)
    _check("chunk_days=5 is accepted", job._trips_chunk_days({"chunk_days": 5}) == 5)

    try:
        job._trips_chunk_days({"chunk_days": 6})
    except ValueError as exc:
        _check("chunk_days=6 fails clearly", "chunk_days must be <= 5" in str(exc), str(exc))
    else:
        _check("chunk_days=6 fails clearly", False)

    try:
        job._trips_chunk_days({"chunk_days": 0})
    except ValueError as exc:
        _check("chunk_days=0 fails clearly", "chunk_days must be > 0" in str(exc), str(exc))
    else:
        _check("chunk_days=0 fails clearly", False)


class FakeLogClient:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def log(self, level: str, _kind: str, _source: str, message: str, *, run_id: str, context: dict) -> None:
        self.logs.append({"level": level, "message": message, "run_id": run_id, "context": dict(context)})


class FakeTelematics:
    page_limit = 1000

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.calls: list[dict] = []

    def fetch_trips(self, *, window_start_ts: datetime, window_end_ts: datetime, incl_private: bool) -> list[dict]:
        self.calls.append({
            "window_start_ts": window_start_ts,
            "window_end_ts": window_end_ts,
            "incl_private": incl_private,
        })
        if self.fail_on_call == len(self.calls):
            raise TelematicsProviderSafetyError(
                "MAX_RETRIES",
                "Exceeded TELEMATICS_PROVIDER_MAX_RETRIES (2) for /trips",
                context={"endpoint": "/trips"},
            )
        return [{"trip_id": len(self.calls), "registration": "REG-A"}]


def test_fetch_trips_called_once_per_chunk() -> None:
    fake_client = FakeLogClient()
    fake_provider = FakeTelematics()
    rows, summaries = job._fetch_trips_in_chunks(
        telematics=fake_provider,
        client=fake_client,
        run_id="run-1",
        client_id="client-1",
        window_start_ts=_ts("2026-04-01T00:00:00Z"),
        window_end_ts=_ts("2026-04-07T00:00:00Z"),
        chunk_days=5,
        incl_private=True,
    )
    _check("fetch_trips is called once per chunk", len(fake_provider.calls) == 2, repr(fake_provider.calls))
    _check("fetch_trips is not called once for the whole original window",
           fake_provider.calls[0]["window_end_ts"] != _ts("2026-04-07T00:00:00Z"), repr(fake_provider.calls))
    _check("chunk fetch aggregates rows across chunks", len(rows) == 2 and len(summaries) == 2)


def test_provider_error_includes_chunk_context() -> None:
    fake_client = FakeLogClient()
    fake_provider = FakeTelematics(fail_on_call=2)
    try:
        job._fetch_trips_in_chunks(
            telematics=fake_provider,
            client=fake_client,
            run_id="run-1",
            client_id="client-1",
            window_start_ts=_ts("2026-04-01T00:00:00Z"),
            window_end_ts=_ts("2026-04-07T00:00:00Z"),
            chunk_days=5,
            incl_private=True,
        )
    except TelematicsProviderSafetyError as exc:
        context = exc.context
        _check("provider error context includes chunk_start_ts", context.get("chunk_start_ts") == "2026-04-06T00:00:00+00:00", repr(context))
        _check("provider error context includes chunk_end_ts", context.get("chunk_end_ts") == "2026-04-07T00:00:00+00:00", repr(context))
        _check("provider error context includes original window", context.get("original_window_start_ts") == "2026-04-01T00:00:00+00:00", repr(context))
        _check("provider error message includes chunk context",
               "chunk_start_ts=2026-04-06T00:00:00+00:00" in str(exc)
               and "chunk_end_ts=2026-04-07T00:00:00+00:00" in str(exc),
               str(exc))
    else:
        _check("provider error includes chunk context", False)


def main() -> int:
    test_chunk_boundaries()
    test_chunk_days_validation()
    test_fetch_trips_called_once_per_chunk()
    test_provider_error_includes_chunk_context()
    if FAILURES:
        print("\nFailures:")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print("\nAll trip chunking manual tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
