#!/usr/bin/env python3
"""Manual tests for speeding violation counts and notification RPM helpers in
`jobs.api.telematics.sync_trips_and_speeding`.

Run from repo root:

    python3 ops/tests_manual/test_workflow_a_speed_bucket_and_notification_type.py
"""
from __future__ import annotations

import os
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


_install_stub("requests")
_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402
from jobs.api.telematics.provider_safety import (  # noqa: E402
    TelematicsProviderSafetyError,
    ProviderRunBudget,
    SafetyLimits,
)

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


def _trip(provider_trip_id: int, start: str, end: str, registration: str = "REG-A") -> dict:
    return {
        "provider_trip_id": provider_trip_id,
        "registration": registration,
        "vehicle_id": "veh-1",
        "start_ts": _ts(start),
        "end_ts": _ts(end),
    }


def _event(when: str, speed, registration: str = " reg-a ") -> dict:
    return {
        "registration": registration,
        "event_ts": _ts(when),
        "speed": speed,
    }


def test_speed_bucket_boundaries() -> None:
    cases = [
        (139, None),
        (140, "speeding_140_160_count"),
        (159, "speeding_140_160_count"),
        (160, "speeding_160_170_count"),
        (169, "speeding_160_170_count"),
        (170, "speeding_170_plus_count"),
        (180, "speeding_170_plus_count"),
    ]
    for speed, expected in cases:
        got = job._speed_bucket(speed)
        _check(f"_speed_bucket({speed}) == {expected!r}", got == expected, f"got={got!r}")


def test_each_distinct_timestamp_counts_as_one_violation() -> None:
    counts, stats = job._compute_speeding_violation_counts(
        trips=[_trip(1, "2026-04-01T10:00:00Z", "2026-04-01T10:01:00Z")],
        vehicle_events=[
            _event("2026-04-01T10:00:01Z", 140),
            _event("2026-04-01T10:00:02Z", 141),
            _event("2026-04-01T10:00:03Z", 140),
        ],
    )
    _check("three consecutive timestamps count as three violations",
           counts[1]["speeding_140_160_count"] == 3,
           f"counts={counts!r}, stats={stats!r}")
    _check("violation stats count three created and matched",
           stats["speeding_violations_created"] == 3
           and stats["speeding_violations_matched"] == 3,
           f"stats={stats!r}")


def test_same_registration_timestamp_rows_each_count() -> None:
    counts, stats = job._compute_speeding_violation_counts(
        trips=[_trip(1, "2026-04-01T10:00:00Z", "2026-04-01T10:01:00Z")],
        vehicle_events=[
            _event("2026-04-01T10:00:01Z", 145),
            _event("2026-04-01T10:00:01Z", 145, registration="REG-A"),
            _event("2026-04-01T10:00:01Z", 145, registration=" reg-a "),
        ],
    )
    _check("same registration+timestamp rows count separately",
           counts[1]["speeding_140_160_count"] == 3
           and stats["speeding_violations_created"] == 3,
           f"counts={counts!r}, stats={stats!r}")


def test_violation_bucket_boundaries() -> None:
    counts, stats = job._compute_speeding_violation_counts(
        trips=[_trip(1, "2026-04-01T10:00:00Z", "2026-04-01T10:01:00Z")],
        vehicle_events=[
            _event("2026-04-01T10:00:01Z", 159),
            _event("2026-04-01T10:00:02Z", 160),
            _event("2026-04-01T10:00:03Z", 169),
            _event("2026-04-01T10:00:04Z", 170),
        ],
    )
    _check("159 counts in speeding_140_160_count",
           counts[1]["speeding_140_160_count"] == 1,
           f"counts={counts!r}")
    _check("160 and 169 count in speeding_160_170_count",
           counts[1]["speeding_160_170_count"] == 2,
           f"counts={counts!r}")
    _check("170 counts in speeding_170_plus_count",
           counts[1]["speeding_170_plus_count"] == 1,
           f"counts={counts!r}")
    _check("all boundary samples matched",
           stats["speeding_violations_matched"] == 4,
           f"stats={stats!r}")


def test_distinct_timestamps_one_second_apart_are_separate() -> None:
    violations = job._build_speeding_violations([
        _event("2026-04-01T10:00:01Z", 140),
        _event("2026-04-01T10:00:02Z", 141),
        _event("2026-04-01T10:00:03Z", 140),
    ])
    _check("timestamps differing by one second are separate violations",
           len(violations) == 3,
           f"violations={violations!r}")


def test_trip_assignment_and_overlap_tie_break() -> None:
    trips = [
        _trip(1, "2026-04-01T09:00:00Z", "2026-04-01T11:00:00Z"),
        _trip(2, "2026-04-01T09:45:00Z", "2026-04-01T10:15:00Z"),
    ]
    counts, stats = job._compute_speeding_violation_counts(
        trips=trips,
        vehicle_events=[
            _event("2026-04-01T10:00:00Z", 160),
            _event("2026-04-01T12:00:00Z", 170),
        ],
    )
    _check("overlap tie-break chooses shortest containing trip",
           counts[1]["speeding_160_170_count"] == 0
           and counts[2]["speeding_160_170_count"] == 1,
           f"counts={counts!r}")
    _check("outside-trip violation is unmatched",
           stats["speeding_violations_unmatched"] == 1,
           f"stats={stats!r}")


def test_notifications_independence_for_speeding_counts() -> None:
    counts, stats = job._compute_speeding_violation_counts(
        trips=[_trip(1, "2026-04-01T08:00:00Z", "2026-04-01T09:00:00Z")],
        vehicle_events=[_event("2026-04-01T08:30:00Z", 150)],
    )
    _check("empty notifications are irrelevant to speeding counts",
           counts[1]["speeding_140_160_count"] == 1,
           f"counts={counts!r}, stats={stats!r}")


def test_fleet_events_filter_by_trip_registration_and_speed() -> None:
    filtered, stats = job._filter_fleet_speeding_events(
        vehicle_events=[
            _event("2026-04-01T08:00:00Z", 139, registration="REG-A"),
            _event("2026-04-01T08:00:01Z", 140, registration=" reg-a "),
            _event("2026-04-01T08:00:02Z", 170, registration="REG-B"),
            _event("2026-04-01T08:00:03Z", 180, registration="OTHER"),
        ],
        trip_registrations_norm={"REG-A", "REG-B"},
    )
    _check("fleet filter keeps only trip registrations before speed filter",
           stats["rows_kept_after_registration_filter"] == 3,
           f"stats={stats!r}")
    _check("fleet filter drops speed <140 after registration filter",
           stats["rows_kept_after_speed_filter"] == 2 and len(filtered) == 2,
           f"filtered={filtered!r}, stats={stats!r}")


def test_fleet_event_request_windows_chunk_without_boundary_overlap() -> None:
    windows = list(job._iter_vehicle_events_fleet_request_windows(
        _ts("2026-04-26T00:00:00Z"),
        _ts("2026-04-26T12:00:00Z"),
        chunk_delta=timedelta(hours=4),
    ))
    _check("12h fleet window produces three 4h requests",
           len(windows) == 3,
           f"windows={windows!r}")
    if windows:
        start, request_end, exclusive_end = windows[0]
        _check("first chunk request start is unchanged",
               start == _ts("2026-04-26T00:00:00Z"),
               f"start={start!r}")
        _check("non-final chunk request ends one second before exclusive end",
               request_end == _ts("2026-04-26T03:59:59Z")
               and exclusive_end == _ts("2026-04-26T04:00:00Z")
               and windows[1][0] == _ts("2026-04-26T04:00:00Z"),
               f"windows={windows!r}")
        _check("final chunk keeps exact requested end",
               windows[-1][1] == _ts("2026-04-26T12:00:00Z"),
               f"windows={windows!r}")


def test_fleet_event_request_windows_partial_unchanged() -> None:
    windows = list(job._iter_vehicle_events_fleet_request_windows(
        _ts("2026-04-26T08:00:00Z"),
        _ts("2026-04-26T09:00:00Z"),
        chunk_delta=timedelta(hours=4),
    ))
    _check("partial <24h window is unchanged",
           windows == [(
               _ts("2026-04-26T08:00:00Z"),
               _ts("2026-04-26T09:00:00Z"),
               _ts("2026-04-26T09:00:00Z"),
           )],
           f"windows={windows!r}")


def test_fleet_event_request_windows_skip_zero_length() -> None:
    windows = list(job._iter_vehicle_events_fleet_request_windows(
        _ts("2026-04-26T00:00:00Z"),
        _ts("2026-04-26T00:00:00Z"),
        chunk_delta=timedelta(hours=4),
    ))
    _check("zero-length window sends no requests",
           windows == [],
           f"windows={windows!r}")


def test_vehicle_events_fleet_limit_env_default_and_override() -> None:
    old_limit = os.environ.get(job.VEHICLE_EVENTS_FLEET_LIMIT_ENV)
    old_max_pages = os.environ.get(job.VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV)
    try:
        os.environ.pop(job.VEHICLE_EVENTS_FLEET_LIMIT_ENV, None)
        os.environ.pop(job.VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV, None)
        _check("fleet vehicle events limit defaults to 1000",
               job._vehicle_events_fleet_limit() == 1000,
               f"limit={job._vehicle_events_fleet_limit()}")
        _check("fleet vehicle events max_pages defaults to 500",
               job._vehicle_events_fleet_max_pages() == 500,
               f"max_pages={job._vehicle_events_fleet_max_pages()}")

        os.environ[job.VEHICLE_EVENTS_FLEET_LIMIT_ENV] = "500"
        os.environ[job.VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV] = "25"
        _check("fleet vehicle events limit can be configured",
               job._vehicle_events_fleet_limit() == 500,
               f"limit={job._vehicle_events_fleet_limit()}")
        os.environ[job.VEHICLE_EVENTS_FLEET_LIMIT_ENV] = "2000"
        _check("fleet vehicle events limit is capped at 1000",
               job._vehicle_events_fleet_limit() == 1000,
               f"limit={job._vehicle_events_fleet_limit()}")
        os.environ[job.VEHICLE_EVENTS_FLEET_LIMIT_ENV] = "500"
        _check("fleet vehicle events max_pages can be configured",
               job._vehicle_events_fleet_max_pages() == 25,
               f"max_pages={job._vehicle_events_fleet_max_pages()}")
    finally:
        if old_limit is None:
            os.environ.pop(job.VEHICLE_EVENTS_FLEET_LIMIT_ENV, None)
        else:
            os.environ[job.VEHICLE_EVENTS_FLEET_LIMIT_ENV] = old_limit
        if old_max_pages is None:
            os.environ.pop(job.VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV, None)
        else:
            os.environ[job.VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV] = old_max_pages


def test_vehicle_events_adaptive_config_defaults_and_override() -> None:
    tracked = [
        job.VEHICLE_EVENTS_CHUNK_HOURS_ENV,
        job.VEHICLE_EVENTS_MIN_CHUNK_MINUTES_ENV,
        job.VEHICLE_EVENTS_RATE_LIMIT_RPS_ENV,
    ]
    old = {name: os.environ.get(name) for name in tracked}
    try:
        for name in tracked:
            os.environ.pop(name, None)
        _check("adaptive vehicle events chunk defaults to 4h",
               job._vehicle_events_chunk_delta() == timedelta(hours=4),
               f"chunk={job._vehicle_events_chunk_delta()}")
        _check("adaptive vehicle events min chunk defaults to 30m",
               job._vehicle_events_min_chunk_delta() == timedelta(minutes=30),
               f"min={job._vehicle_events_min_chunk_delta()}")
        _check("adaptive vehicle events rate limit defaults to 2.5 rps",
               job._vehicle_events_rate_limit_rps() == 2.5,
               f"rps={job._vehicle_events_rate_limit_rps()}")

        os.environ[job.VEHICLE_EVENTS_CHUNK_HOURS_ENV] = "2"
        os.environ[job.VEHICLE_EVENTS_MIN_CHUNK_MINUTES_ENV] = "15"
        os.environ[job.VEHICLE_EVENTS_RATE_LIMIT_RPS_ENV] = "2"
        _check("adaptive vehicle events config can be overridden",
               job._vehicle_events_chunk_delta() == timedelta(hours=2)
               and job._vehicle_events_min_chunk_delta() == timedelta(minutes=15)
               and job._vehicle_events_rate_limit_rps() == 2.0,
               f"chunk={job._vehicle_events_chunk_delta()}, min={job._vehicle_events_min_chunk_delta()}, rps={job._vehicle_events_rate_limit_rps()}")
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_skip_vehicle_events_param_is_wired() -> None:
    src = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check("skip_vehicle_events job param is parsed",
           '_param_bool(params, "skip_vehicle_events", False)' in src,
           "")
    _check("skip_vehicle_events remains fatal before fleet event fetch",
           "DB upsert skipped because skip_vehicle_events=true would produce incomplete event enrichment" in src
           and "SKIP_VEHICLE_EVENTS_INCOMPLETE_ENRICHMENT" in src,
           "")


class FakeAdaptiveVehicleEventsProvider:
    def __init__(self, *, fail_first: bool = False, always_fail: bool = False):
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.calls: list[dict] = []

    def fetch_vehicle_events_fleet(self, **kwargs):
        self.calls.append(dict(kwargs))
        start = kwargs["start_timestamp"]
        end = kwargs["end_timestamp"]
        duration = end - start
        if self.always_fail:
            raise TelematicsProviderSafetyError(
                "HTTP_RETRY_EXHAUSTED",
                "simulated timeout",
                context={"attempts": 3, "last_error": "RequestsTimeout"},
            )
        if self.fail_first and len(self.calls) == 1 and duration > timedelta(hours=2):
            raise TelematicsProviderSafetyError(
                "HTTP_RETRY_EXHAUSTED",
                "simulated timeout",
                context={"attempts": 3, "last_error": "RequestsTimeout"},
            )
        event = {
            "event_id": len(self.calls),
            "registration": "REG-A",
            "event_ts": start,
            "speed": 145,
        }
        return [event], {"pages_fetched": 1, "records_fetched": 1}


def _fallback_config(
    *,
    enabled: bool = False,
    max_registrations: int = 100,
    max_chunks: int = 1,
    max_requests_per_run: int = 100,
    explicit: bool = True,
    min_minutes: int = 5,
) -> job.RegistrationFallbackConfig:
    return job.RegistrationFallbackConfig(
        enabled=enabled,
        rps=10.0,
        max_registrations=max_registrations,
        max_chunks=max_chunks,
        max_requests_per_run=max_requests_per_run,
        max_requests_per_run_explicit=explicit,
        min_chunk_delta=timedelta(minutes=min_minutes),
        progress_interval=1,
    )


def test_adaptive_vehicle_events_reduces_chunk_after_timeout() -> None:
    provider = FakeAdaptiveVehicleEventsProvider(fail_first=True)
    logs: list[tuple[str, str, dict]] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_adaptive(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T06:00:00Z"),
        initial_chunk_delta=timedelta(hours=4),
        min_chunk_delta=timedelta(minutes=30),
        fallback_config=_fallback_config(enabled=False),
        fallback_registrations=[],
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: logs.append((level, message, context)),
    ))
    requested_windows = [
        (call["start_timestamp"], call["end_timestamp"])
        for call in provider.calls
    ]
    _check("adaptive fetch retries failed 4h chunk as 2h chunks",
           requested_windows[0] == (_ts("2026-04-26T00:00:00Z"), _ts("2026-04-26T03:59:59Z"))
           and requested_windows[1] == (_ts("2026-04-26T00:00:00Z"), _ts("2026-04-26T01:59:59Z"))
           and requested_windows[2] == (_ts("2026-04-26T02:00:00Z"), _ts("2026-04-26T03:59:59Z"))
           and requested_windows[3] == (_ts("2026-04-26T04:00:00Z"), _ts("2026-04-26T06:00:00Z")),
           f"requested_windows={requested_windows!r}")
    _check("adaptive fetch keeps successful reduced chunk size",
           len(chunks) == 3 and sum(len(events) for events, _ in chunks) == 3,
           f"chunks={chunks!r}")
    _check("adaptive fetch logs chunk reduction",
           any(message == "Fleet vehicle events chunk failed; reducing chunk size"
               and context.get("new_chunk_minutes") == 120.0
               for _, message, context in logs),
           f"logs={logs!r}")


def test_adaptive_vehicle_events_respects_min_chunk() -> None:
    provider = FakeAdaptiveVehicleEventsProvider(always_fail=True)
    logs: list[tuple[str, str, dict]] = []
    try:
        list(job._iter_fetch_vehicle_events_fleet_adaptive(
            provider=provider,
            window_start_ts=_ts("2026-04-26T00:00:00Z"),
            window_end_ts=_ts("2026-04-26T02:00:00Z"),
            initial_chunk_delta=timedelta(hours=1),
            min_chunk_delta=timedelta(minutes=30),
            fallback_config=_fallback_config(enabled=False),
            fallback_registrations=[],
            limit=1000,
            max_pages=500,
            timeout_s=30,
            log_fn=lambda level, message, context: logs.append((level, message, context)),
        ))
        _check("adaptive fetch raises after min chunk failure", False)
    except TelematicsProviderSafetyError:
        requested_durations = [
            call["end_timestamp"] - call["start_timestamp"]
            for call in provider.calls
        ]
        _check("adaptive fetch does not shrink below min chunk",
               requested_durations[0] == timedelta(minutes=59, seconds=59)
               and requested_durations[1] == timedelta(minutes=29, seconds=59)
               and len(provider.calls) == 2,
               f"durations={requested_durations!r}, calls={provider.calls!r}")
        _check("adaptive fetch logs terminal chunk failure",
               any(message == "Fleet vehicle events chunk failed at minimum/safety limit"
                   for _, message, _ in logs),
               f"logs={logs!r}")


class FakeFallbackVehicleEventsProvider:
    def __init__(
        self,
        *,
        fleet_success: bool = False,
        fail_large_registration: str | None = None,
        fail_all_registration: str | None = None,
    ):
        self.fleet_success = fleet_success
        self.fail_large_registration = fail_large_registration
        self.fail_all_registration = fail_all_registration
        self.fleet_calls: list[dict] = []
        self.registration_calls: list[dict] = []
        self.rate_limit_rps = None
        self.budget = ProviderRunBudget(limits=SafetyLimits(
            max_requests_per_run=1000,
            max_requests_per_endpoint=1000,
            max_requests_per_subwindow=1000,
            max_pages_per_subwindow=1000,
        ))

    def fetch_vehicle_events_fleet(self, **kwargs):
        self.fleet_calls.append(dict(kwargs))
        if self.fleet_success:
            start = kwargs["start_timestamp"]
            return ([{
                "event_id": "fleet-1",
                "registration": "REG-A",
                "event_ts": start,
                "speed": 145,
            }], {"pages_fetched": 1, "records_fetched": 1})
        raise TelematicsProviderSafetyError(
            "HTTP_ERROR",
            "simulated fleet 500",
            context={"status_code": 500, "response_body_text": "simulated"},
        )

    def fetch_vehicle_events_registration(self, **kwargs):
        self.registration_calls.append(dict(kwargs))
        registration = kwargs["registration"]
        start = kwargs["start_timestamp"]
        end = kwargs["end_timestamp"]
        self.budget.record_request_issued(
            path="/vehicles/events:registration",
            sub_window_label=kwargs["sub_window_label"],
        )
        if registration == self.fail_all_registration:
            raise TelematicsProviderSafetyError(
                "HTTP_ERROR",
                "simulated registration 500",
                context={"status_code": 500, "registration": registration, "response_body_text": "simulated"},
            )
        if registration == self.fail_large_registration and job._event_window_seconds(start, end) > 300:
            raise TelematicsProviderSafetyError(
                "HTTP_ERROR",
                "simulated large-window registration 500",
                context={"status_code": 500, "registration": registration, "response_body_text": "simulated"},
            )
        return ([{
            "event_id": f"{registration}-{len(self.registration_calls)}",
            "registration": registration,
            "event_ts": start,
            "speed": 145,
        }], {"pages_fetched": 1, "records_fetched": 1, "stopped_by_max_pages": False})


class FakeBestEffortFleetProvider:
    def __init__(self, *, fail_status: int | None = None, fail_above_seconds: int | None = None):
        self.fail_status = fail_status
        self.fail_above_seconds = fail_above_seconds
        self.fleet_calls: list[dict] = []
        self.registration_calls: list[dict] = []
        self.rate_limit_rps = None
        self.budget = ProviderRunBudget(limits=SafetyLimits(
            max_requests_per_run=1000,
            max_requests_per_endpoint=1000,
            max_requests_per_subwindow=1000,
            max_pages_per_subwindow=1000,
        ))

    def fetch_vehicle_events_fleet(self, **kwargs):
        self.fleet_calls.append(dict(kwargs))
        start = kwargs["start_timestamp"]
        end = kwargs["end_timestamp"]
        duration_s = job._event_window_seconds(start, end)
        if self.fail_status is not None:
            raise TelematicsProviderSafetyError(
                "HTTP_ERROR",
                f"simulated fleet {self.fail_status}",
                context={"status_code": self.fail_status, "response_body_text": "simulated"},
            )
        if self.fail_above_seconds is not None and duration_s > self.fail_above_seconds:
            raise TelematicsProviderSafetyError(
                "HTTP_ERROR",
                "simulated fleet 500",
                context={"status_code": 500, "response_body_text": "simulated"},
            )
        return ([{
            "event_id": f"fleet-{len(self.fleet_calls)}",
            "registration": "REG-A",
            "event_ts": start,
            "speed": 145,
        }], {"pages_fetched": 1, "records_fetched": 1, "stopped_by_max_pages": False})


def _best_effort_config(
    *,
    min_fleet_minutes: int = 5,
    min_registration_minutes: int = 5,
    max_gaps: int = 100,
    max_depth: int = 8,
) -> job.BestEffortConfig:
    return job.BestEffortConfig(
        min_fleet_chunk_delta=timedelta(minutes=min_fleet_minutes),
        min_registration_chunk_delta=timedelta(minutes=min_registration_minutes),
        max_gaps_per_run=max_gaps,
        max_split_depth=max_depth,
    )


def test_fleet_success_does_not_call_registration_fallback() -> None:
    provider = FakeFallbackVehicleEventsProvider(fleet_success=True)
    logs: list[tuple[str, str, dict]] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_adaptive(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:30:00Z"),
        initial_chunk_delta=timedelta(minutes=30),
        min_chunk_delta=timedelta(minutes=30),
        fallback_config=_fallback_config(enabled=True),
        fallback_registrations=["REG-A", "REG-B"],
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: logs.append((level, message, context)),
    ))
    _check("fleet success yields one fleet chunk",
           len(chunks) == 1 and chunks[0][1].get("source") == "fleet",
           f"chunks={chunks!r}")
    _check("no per-registration calls unless fallback is triggered",
           provider.registration_calls == [],
           f"registration_calls={provider.registration_calls!r}")


def test_fleet_failure_fallback_all_registrations_success() -> None:
    provider = FakeFallbackVehicleEventsProvider()
    logs: list[tuple[str, str, dict]] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_adaptive(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:30:00Z"),
        initial_chunk_delta=timedelta(minutes=30),
        min_chunk_delta=timedelta(minutes=30),
        fallback_config=_fallback_config(enabled=True, max_requests_per_run=10),
        fallback_registrations=["REG-A", "REG-B"],
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: logs.append((level, message, context)),
    ))
    _check("fleet failure starts registration fallback and succeeds",
           len(chunks) == 1
           and chunks[0][1].get("source") == "registration_fallback"
           and len(chunks[0][0]) == 2,
           f"chunks={chunks!r}")
    _check("fallback progress logs are emitted",
           any(message == "Registration fallback progress" for _, message, _ in logs)
           and any(message == "Registration fallback completed for failed fleet vehicle-events chunk"
                   and context.get("complete_event_enrichment") is True
                   for _, message, context in logs),
           f"logs={logs!r}")


def test_registration_fallback_subchunks_failed_large_registration() -> None:
    provider = FakeFallbackVehicleEventsProvider(fail_large_registration="REG-B")
    logs: list[tuple[str, str, dict]] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_adaptive(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:30:00Z"),
        initial_chunk_delta=timedelta(minutes=30),
        min_chunk_delta=timedelta(minutes=30),
        fallback_config=_fallback_config(enabled=True, max_requests_per_run=50, min_minutes=5),
        fallback_registrations=["REG-A", "REG-B"],
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: logs.append((level, message, context)),
    ))
    _check("failed large registration succeeds via smaller subchunks",
           len(chunks) == 1
           and chunks[0][1].get("source") == "registration_fallback"
           and chunks[0][1].get("fallback_subchunks_fetched", 0) > 0,
           f"chunks={chunks!r}")
    _check("fallback subchunk progress is logged",
           any(message == "Registration fallback subchunk progress" for _, message, _ in logs),
           f"logs={logs!r}")


def test_registration_fallback_min_subchunk_failure_aborts() -> None:
    provider = FakeFallbackVehicleEventsProvider(fail_all_registration="REG-B")
    logs: list[tuple[str, str, dict]] = []
    try:
        list(job._iter_fetch_vehicle_events_fleet_adaptive(
            provider=provider,
            window_start_ts=_ts("2026-04-26T00:00:00Z"),
            window_end_ts=_ts("2026-04-26T00:30:00Z"),
            initial_chunk_delta=timedelta(minutes=30),
            min_chunk_delta=timedelta(minutes=30),
            fallback_config=_fallback_config(enabled=True, max_requests_per_run=50, min_minutes=5),
            fallback_registrations=["REG-A", "REG-B"],
            limit=1000,
            max_pages=500,
            timeout_s=30,
            log_fn=lambda level, message, context: logs.append((level, message, context)),
        ))
        _check("registration fallback min subchunk failure aborts", False)
    except TelematicsProviderSafetyError:
        _check("registration fallback min subchunk failure aborts before success",
               any("DB upsert" in message and context.get("complete_event_enrichment") is False
                   for _, message, context in logs),
               f"logs={logs!r}")


def test_registration_fallback_budget_exceeded_before_requests() -> None:
    provider = FakeFallbackVehicleEventsProvider()
    logs: list[tuple[str, str, dict]] = []
    try:
        list(job._iter_fetch_vehicle_events_fleet_adaptive(
            provider=provider,
            window_start_ts=_ts("2026-04-26T00:00:00Z"),
            window_end_ts=_ts("2026-04-26T00:30:00Z"),
            initial_chunk_delta=timedelta(minutes=30),
            min_chunk_delta=timedelta(minutes=30),
            fallback_config=_fallback_config(enabled=True, max_requests_per_run=1),
            fallback_registrations=["REG-A", "REG-B"],
            limit=1000,
            max_pages=500,
            timeout_s=30,
            log_fn=lambda level, message, context: logs.append((level, message, context)),
        ))
        _check("fallback request budget exceeded fails fast", False)
    except TelematicsProviderSafetyError:
        _check("fallback request budget exceeded fails before registration calls",
               provider.registration_calls == []
               and any(context.get("abort_code") == "REGISTRATION_FALLBACK_ESTIMATED_REQUESTS_EXCEED_BUDGET"
                       for _, _, context in logs),
               f"registration_calls={provider.registration_calls!r}, logs={logs!r}")


def test_best_effort_mode_config_defaults_and_param_override() -> None:
    old_mode = os.environ.get(job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENV)
    try:
        os.environ.pop(job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENV, None)
        _check("event enrichment mode defaults to enabled",
               job._event_enrichment_mode({}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED,
               "")
        _check("default event fetch strategy remains strict",
               job._event_fetch_strategy({}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT,
               "")
        os.environ[job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENV] = job.VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT
        _check("legacy audited env keeps event enrichment enabled",
               job._event_enrichment_mode({}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED,
               "")
        _check("event fetch strategy reads legacy env",
               job._event_fetch_strategy({}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
               "")
        _check("event enrichment mode param overrides env",
               job._event_enrichment_mode({"event_enrichment_mode": "disabled"}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED,
               "")
        _check("enabled param maps to strict fetch strategy",
               job._event_fetch_strategy({"event_enrichment_mode": "enabled"}) == job.VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT,
               "")
    finally:
        if old_mode is None:
            os.environ.pop(job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENV, None)
        else:
            os.environ[job.VEHICLE_EVENTS_ENRICHMENT_MODE_ENV] = old_mode


def test_best_effort_fleet_failure_splits_and_keeps_successful_subchunks() -> None:
    provider = FakeBestEffortFleetProvider(fail_above_seconds=300)
    gaps: list[dict] = []
    logs: list[tuple[str, str, dict]] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_best_effort(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:10:00Z"),
        initial_chunk_delta=timedelta(minutes=10),
        min_chunk_delta=timedelta(minutes=5),
        fallback_config=_fallback_config(enabled=False),
        fallback_registrations=[],
        best_effort_config=_best_effort_config(min_fleet_minutes=5),
        gaps=gaps,
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: logs.append((level, message, context)),
    ))
    _check("best-effort keeps successful fleet subchunks after split",
           len(chunks) == 1 and len(chunks[0][0]) == 3 and gaps == [],
           f"chunks={chunks!r}, gaps={gaps!r}")
    _check("best-effort fleet split is logged",
           any(message == "Audited best-effort fleet vehicle-events chunk failed; subdividing"
               for _, message, _ in logs),
           f"logs={logs!r}")


def test_best_effort_records_unresolved_min_fleet_gap() -> None:
    provider = FakeBestEffortFleetProvider(fail_status=500)
    gaps: list[dict] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_best_effort(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:05:00Z"),
        initial_chunk_delta=timedelta(minutes=5),
        min_chunk_delta=timedelta(minutes=5),
        fallback_config=_fallback_config(enabled=False),
        fallback_registrations=[],
        best_effort_config=_best_effort_config(min_fleet_minutes=5),
        gaps=gaps,
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: None,
    ))
    _check("best-effort records unresolved min fleet gap",
           len(chunks) == 1
           and chunks[0][0] == []
           and len(gaps) == 1
           and gaps[0]["scope"] == "fleet"
           and gaps[0]["status_code"] == 500,
           f"chunks={chunks!r}, gaps={gaps!r}")


def test_best_effort_registration_recovery_splits_failed_registration() -> None:
    provider = FakeFallbackVehicleEventsProvider(fail_large_registration="REG-B")
    gaps: list[dict] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_best_effort(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:30:00Z"),
        initial_chunk_delta=timedelta(minutes=30),
        min_chunk_delta=timedelta(minutes=30),
        fallback_config=_fallback_config(enabled=True, max_requests_per_run=50, min_minutes=5),
        fallback_registrations=["REG-A", "REG-B"],
        best_effort_config=_best_effort_config(min_fleet_minutes=30, min_registration_minutes=5),
        gaps=gaps,
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: None,
    ))
    _check("best-effort registration recovery keeps successful subwindows",
           len(chunks) == 1
           and chunks[0][1].get("source") == "registration_fallback"
           and len(chunks[0][0]) > 2
           and gaps == [],
           f"chunks={chunks!r}, gaps={gaps!r}")


def test_best_effort_records_unresolved_registration_gap() -> None:
    provider = FakeFallbackVehicleEventsProvider(fail_all_registration="REG-B")
    gaps: list[dict] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_best_effort(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:04:59Z"),
        initial_chunk_delta=timedelta(minutes=5),
        min_chunk_delta=timedelta(minutes=5),
        fallback_config=_fallback_config(enabled=True, max_requests_per_run=20, min_minutes=5),
        fallback_registrations=["REG-A", "REG-B"],
        best_effort_config=_best_effort_config(min_registration_minutes=5),
        gaps=gaps,
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: None,
    ))
    _check("best-effort records unresolved registration gap",
           len(chunks) == 1
           and len(gaps) == 1
           and gaps[0]["scope"] == "registration"
           and gaps[0]["registration"] == "REG-B",
           f"chunks={chunks!r}, gaps={gaps!r}")


def test_best_effort_auth_and_429_fail_fast() -> None:
    for status in (401, 403, 429):
        provider = FakeBestEffortFleetProvider(fail_status=status)
        gaps: list[dict] = []
        try:
            list(job._iter_fetch_vehicle_events_fleet_best_effort(
                provider=provider,
                window_start_ts=_ts("2026-04-26T00:00:00Z"),
                window_end_ts=_ts("2026-04-26T00:05:00Z"),
                initial_chunk_delta=timedelta(minutes=5),
                min_chunk_delta=timedelta(minutes=5),
                fallback_config=_fallback_config(enabled=False),
                fallback_registrations=[],
                best_effort_config=_best_effort_config(),
                gaps=gaps,
                limit=1000,
                max_pages=500,
                timeout_s=30,
                log_fn=lambda level, message, context: None,
            ))
            _check(f"best-effort persistent {status} fails fast", False)
        except TelematicsProviderSafetyError as exc:
            _check(f"best-effort persistent {status} fails fast",
                   exc.code == "INCOMPLETE_EVENT_ENRICHMENT" and gaps == [],
                   f"code={exc.code}, gaps={gaps!r}")


def test_best_effort_summary_and_artifact() -> None:
    gap = {
        "scope": "registration",
        "registration": "REG-B",
        "chunk_start_ts": "2026-04-26T00:00:00+00:00",
        "chunk_end_ts": "2026-04-26T00:04:59+00:00",
        "duration_seconds": 300,
        "endpoint": "/vehicles/events:registration",
        "mode": job.VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
        "failure_code": "HTTP_ERROR",
        "status_code": 500,
        "response_body_summary": "simulated",
        "attempts": None,
        "split_depth": 0,
        "min_chunk_minutes": 5,
    }
    summary = job._vehicle_events_gap_summary([gap])

    class FakeArtifactClient:
        def __init__(self):
            self.uploads: list[tuple[str, str, str]] = []

        def upload_artifact(self, path: str, kind: str, run_id: str):
            self.uploads.append((path, kind, run_id))
            return "artifact-1"

    client = FakeArtifactClient()
    path, artifact_id = job._write_vehicle_events_gap_audit_artifact(
        client=client,
        run_id="run-test",
        client_id="client-test",
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T01:00:00Z"),
        mode=job.VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
        summary=summary,
        gaps=[gap],
    )
    payload = Path(path).read_text()
    _check("best-effort summary marks event enrichment partial",
           summary["event_enrichment_status"] == "partial"
           and summary["complete_event_enrichment"] is False
           and summary["registration_gap_count"] == 1,
           f"summary={summary!r}")
    _check("gap audit artifact is created and uploaded",
           artifact_id == "artifact-1"
           and client.uploads
           and "VEHICLE_EVENTS_GAP_AUDIT" in payload
           and "REG-B" in payload,
           f"path={path!r}, uploads={client.uploads!r}, payload={payload!r}")


def test_best_effort_no_registration_calls_on_fleet_success() -> None:
    provider = FakeBestEffortFleetProvider()
    gaps: list[dict] = []
    chunks = list(job._iter_fetch_vehicle_events_fleet_best_effort(
        provider=provider,
        window_start_ts=_ts("2026-04-26T00:00:00Z"),
        window_end_ts=_ts("2026-04-26T00:05:00Z"),
        initial_chunk_delta=timedelta(minutes=5),
        min_chunk_delta=timedelta(minutes=5),
        fallback_config=_fallback_config(enabled=True, max_requests_per_run=20, min_minutes=5),
        fallback_registrations=["REG-A", "REG-B"],
        best_effort_config=_best_effort_config(),
        gaps=gaps,
        limit=1000,
        max_pages=500,
        timeout_s=30,
        log_fn=lambda level, message, context: None,
    ))
    _check("best-effort no per-registration calls unless recovery is needed",
           len(chunks) == 1 and provider.registration_calls == [] and gaps == [],
           f"registration_calls={provider.registration_calls!r}, gaps={gaps!r}")


def test_extract_notification_type() -> None:
    _check(
        "explicit RPM trigger_description wins over generic type",
        job._extract_notification_type({
            "type": "SPEEDING",
            "trigger_description": "HIGH_RPM",
        }) == "HIGH_RPM",
        "",
    )
    _check(
        "explicit OVERREV trigger_description wins over generic type",
        job._extract_notification_type({
            "type": "SPEEDING",
            "trigger_description": "OVERREV",
        }) == "OVERREV",
        "",
    )
    _check(
        "missing type + trigger_description HIGH_RPM",
        job._extract_notification_type({"trigger_description": "HIGH_RPM"}) == "HIGH_RPM",
        f"got={job._extract_notification_type({'trigger_description': 'HIGH_RPM'})!r}",
    )
    _check(
        "HIGH RPM / HIGH-RPM normalize to HIGH_RPM",
        job._extract_notification_type({"trigger_description": "HIGH RPM"})
        == "HIGH_RPM"
        and job._extract_notification_type({"trigger_description": "HIGH-RPM"}) == "HIGH_RPM",
        "",
    )
    _check(
        "missing type + OVERREV in phrase",
        job._extract_notification_type({
            "trigger_description": "engine OVERREV detected",
        }) == "OVERREV",
        "",
    )
    _check(
        "OVER_REV / OVER REV / OVER-REV normalize to OVERREV",
        job._extract_notification_type({"trigger_description": "OVER_REV"}) == "OVERREV"
        and job._extract_notification_type({"trigger_description": "OVER REV"}) == "OVERREV"
        and job._extract_notification_type({"trigger_description": "OVER-REV"}) == "OVERREV",
        "",
    )
    _check(
        "missing both",
        job._extract_notification_type({}) is None,
        f"got={job._extract_notification_type({})!r}",
    )


def main() -> int:
    test_speed_bucket_boundaries()
    test_each_distinct_timestamp_counts_as_one_violation()
    test_same_registration_timestamp_rows_each_count()
    test_violation_bucket_boundaries()
    test_distinct_timestamps_one_second_apart_are_separate()
    test_trip_assignment_and_overlap_tie_break()
    test_notifications_independence_for_speeding_counts()
    test_fleet_events_filter_by_trip_registration_and_speed()
    test_fleet_event_request_windows_chunk_without_boundary_overlap()
    test_fleet_event_request_windows_partial_unchanged()
    test_fleet_event_request_windows_skip_zero_length()
    test_vehicle_events_fleet_limit_env_default_and_override()
    test_vehicle_events_adaptive_config_defaults_and_override()
    test_skip_vehicle_events_param_is_wired()
    test_adaptive_vehicle_events_reduces_chunk_after_timeout()
    test_adaptive_vehicle_events_respects_min_chunk()
    test_fleet_success_does_not_call_registration_fallback()
    test_fleet_failure_fallback_all_registrations_success()
    test_registration_fallback_subchunks_failed_large_registration()
    test_registration_fallback_min_subchunk_failure_aborts()
    test_registration_fallback_budget_exceeded_before_requests()
    test_best_effort_mode_config_defaults_and_param_override()
    test_best_effort_fleet_failure_splits_and_keeps_successful_subchunks()
    test_best_effort_records_unresolved_min_fleet_gap()
    test_best_effort_registration_recovery_splits_failed_registration()
    test_best_effort_records_unresolved_registration_gap()
    test_best_effort_auth_and_429_fail_fast()
    test_best_effort_summary_and_artifact()
    test_best_effort_no_registration_calls_on_fleet_success()
    test_extract_notification_type()
    if FAILURES:
        print(f"\n{len(FAILURES)} test(s) failed.")
        return 1
    print("\nAll tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
