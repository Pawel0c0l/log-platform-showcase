#!/usr/bin/env python3
"""Regression suite for the Telematics `/vehicles/events` request wire-time contract.

Pure: stdlib plus `requests` exception types only. No network, no database, no
secrets, no host-clock dependence. `zoneinfo` builds Europe/Warsaw fixtures and
reasons about DST transitions.

The contract under test, established by live GET-only probes against DELTA00001
on 2026-08-10 (see the module header of `provider_client.py` and docs/18 §2):

  * `/vehicles/events` request `start_timestamp` / `end_timestamp` are
    Europe/Warsaw local wall-clock;
  * `/vehicles/events` response `event_ts` is UTC.

The load-bearing cases are the same as for `/trips`: because the provider
addresses events by wall-clock only, the repeated Warsaw autumn hour is
ambiguous, and the suite proves that under the *worst-case* provider resolution
of every ambiguous endpoint the requested interval is still a superset of the
intended absolute interval — no absolute interval can be omitted.

Additionally proven here, and not applicable to `/trips`: the endpoint's
24-hour maximum request window is respected after DST widening, on both the
absolute span and the wall-clock span actually written on the wire.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_vehicle_events_wire_time_contract.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.provider_client import (  # noqa: E402
    VEHICLE_EVENTS_MAX_INTENDED_WINDOW,
    VEHICLE_EVENTS_MAX_REQUEST_WINDOW,
    VEHICLE_EVENTS_REQUEST_TIMEZONE_NAME,
    TelematicsFleetProviderClient,
    _normalize_vehicle_event,
    _provider_dt_str,
    _wall_clock_wire_dt_str,
    vehicle_events_wire_window,
)

WARSAW = ZoneInfo(VEHICLE_EVENTS_REQUEST_TIMEZONE_NAME)
WIRE_FMT = "%Y-%m-%d %H:%M:%S"

# Europe/Warsaw DST transitions in 2026 (both at 01:00 UTC).
SPRING_FORWARD_UTC = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
AUTUMN_FALLBACK_UTC = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}" + (f"\n      {detail}" if detail else ""))


def parse_wire(s: str) -> datetime:
    return datetime.strptime(s, WIRE_FMT)


def readings(wire: str) -> list[datetime]:
    """Every UTC instant the provider could mean by one wall-clock wire string.

    Computed independently of the implementation: two instants inside the
    autumn fold, one otherwise, and none at all inside the spring gap.
    """
    wall = parse_wire(wire)
    out: list[datetime] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=WARSAW, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(WARSAW).replace(tzinfo=None) != wall:
            continue
        if candidate not in out:
            out.append(candidate)
    return sorted(out)


def assert_covers(label: str, start: datetime, end: datetime) -> tuple[str, str]:
    """The core safety invariant, checked on the worst-case provider reading.

        max(readings(start_str)) <= start   and   min(readings(end_str)) >= end
    """
    s, e, _ctx = vehicle_events_wire_window(start, end)
    rs, re_ = readings(s), readings(e)
    check(
        f"{label}: no absolute interval can be omitted",
        bool(rs) and bool(re_) and max(rs) <= start and min(re_) >= end,
        f"wire={s!r}..{e!r} readings_start={rs} readings_end={re_} "
        f"intended={start.isoformat()}..{end.isoformat()}",
    )
    return s, e


# ---------------------------------------------------------------------------
# 1. The live probe. This is the highest-value assertion in the file: it pins
#    the implementation directly to observed provider behaviour.
#
#    DELTA00001 / EL5JV96 / provider trip 432316079, 2026-06-29.
#    Variant A (UTC serialization, `07:58:32`..`08:45:57`) returned 14 rows all
#    at 06:43:54–06:45:57 UTC — `minus_offset`, zero rows in the intended
#    interval. Variant B (`09:58:32`..`10:45:57`) returned 100 rows all inside
#    the intended interval, 19 of the pre-registered expected evidence type.
# ---------------------------------------------------------------------------
print("\n-- live probe (DELTA00001 / EL5JV96 / 2026-06-29) --")

PROBE_START = datetime(2026, 6, 29, 7, 58, 32, tzinfo=timezone.utc)
PROBE_END = datetime(2026, 6, 29, 8, 45, 57, tzinfo=timezone.utc)
PROBE_WIRE_START = "2026-06-29 09:58:32"
PROBE_WIRE_END = "2026-06-29 10:45:57"
PROBE_OLD_WIRE_START = "2026-06-29 07:58:32"
PROBE_OLD_WIRE_END = "2026-06-29 08:45:57"

probe_s, probe_e, probe_ctx = vehicle_events_wire_window(PROBE_START, PROBE_END)
check(
    "live probe: Variant B wire window is emitted",
    (probe_s, probe_e) == (PROBE_WIRE_START, PROBE_WIRE_END),
    f"got {probe_s!r}..{probe_e!r}",
)
check(
    "live probe: Variant A (UTC projection) wire window is NOT emitted",
    probe_s != PROBE_OLD_WIRE_START and probe_e != PROBE_OLD_WIRE_END,
    f"got {probe_s!r}..{probe_e!r}",
)
check(
    "live probe: the old serialization is what _provider_dt_str would have sent",
    _provider_dt_str(PROBE_START) == PROBE_OLD_WIRE_START
    and _provider_dt_str(PROBE_END) == PROBE_OLD_WIRE_END,
)
check("live probe: no DST widening in June", probe_ctx["dst_widened_seconds"] == 0)
check(
    "live probe: diagnostics name the request timezone",
    probe_ctx["vehicle_events_request_timezone"] == "Europe/Warsaw",
)


# ---------------------------------------------------------------------------
# 2. Both request paths. The registration fallback runs exactly when the fleet
#    path is failing, so a divergence there would silently change which events
#    a degraded run collects.
# ---------------------------------------------------------------------------
print("\n-- both /vehicles/events request paths --")


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload
        self.status_code = 200
        self.text = str(payload)
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads_by_page):
        self.payloads_by_page = payloads_by_page
        self.calls: list[dict] = []
        self.auth = None

    def get(self, url: str, *, params: dict, timeout: int):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        payload = self.payloads_by_page[params["page"]]
        if isinstance(payload, BaseException):
            raise payload
        return FakeResponse(payload)


def build_client(payloads_by_page, **kwargs):
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
        page_limit=2,
        **kwargs,
    )
    fake = FakeSession(payloads_by_page)
    client._session = fake
    return client, fake


ONE_PAGE = {
    1: {
        "data": [{
            "event_id": 1,
            "registration": "EL5JV96",
            "vehicle_id": 7,
            # Response timestamps stay UTC — asserted in section 6.
            "event_ts": "2026-06-29 08:00:33",
            "speed": 148,
        }],
        "meta": {"current_page": 1, "last_page": 1},
    },
}

fleet_client, fleet_fake = build_client(dict(ONE_PAGE))
fleet_rows = fleet_client.fetch_vehicle_events_fleet(
    start_timestamp=PROBE_START,
    end_timestamp=PROBE_END,
    sub_window_label="probe-fleet",
    limit=1000,
    max_pages=500,
)
fleet_params = fleet_fake.calls[0]["params"]

reg_client, reg_fake = build_client(dict(ONE_PAGE))
reg_rows = reg_client.fetch_vehicle_events_registration(
    registration="EL5JV96",
    start_timestamp=PROBE_START,
    end_timestamp=PROBE_END,
    sub_window_label="probe-registration",
    limit=1000,
    max_pages=500,
)
reg_params = reg_fake.calls[0]["params"]

check(
    "fleet path emits the live-probe Warsaw wall-clock window",
    fleet_params["start_timestamp"] == PROBE_WIRE_START
    and fleet_params["end_timestamp"] == PROBE_WIRE_END,
    f"params={fleet_params!r}",
)
check(
    "registration path emits the live-probe Warsaw wall-clock window",
    reg_params["start_timestamp"] == PROBE_WIRE_START
    and reg_params["end_timestamp"] == PROBE_WIRE_END,
    f"params={reg_params!r}",
)
check(
    "both paths emit identical wire windows for the same intended interval",
    (fleet_params["start_timestamp"], fleet_params["end_timestamp"])
    == (reg_params["start_timestamp"], reg_params["end_timestamp"]),
)
check(
    "neither path emits the old UTC projection",
    PROBE_OLD_WIRE_START not in (fleet_params["start_timestamp"], reg_params["start_timestamp"])
    and PROBE_OLD_WIRE_END not in (fleet_params["end_timestamp"], reg_params["end_timestamp"]),
)
check(
    "registration path still sends the registration filter",
    reg_params.get("registration") == "EL5JV96" and "registration" not in fleet_params,
    f"fleet={fleet_params!r} reg={reg_params!r}",
)
check(
    "both paths use the fleet /vehicles/events URL",
    fleet_fake.calls[0]["url"] == "https://fleet.example.test/vehicles/events"
    and reg_fake.calls[0]["url"] == "https://fleet.example.test/vehicles/events",
)


# ---------------------------------------------------------------------------
# 3. Ordinary summer and winter windows.
# ---------------------------------------------------------------------------
print("\n-- summer / winter --")

summer_start = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)
summer_end = datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)
s, e, ctx = vehicle_events_wire_window(summer_start, summer_end)
check(
    "summer (CEST, UTC+2): window is shifted forward by two hours",
    (s, e) == ("2026-07-21 16:30:00", "2026-07-21 17:30:00"),
    f"got {s!r}..{e!r}",
)
check("summer: no DST widening", ctx["dst_widened_seconds"] == 0)
check("summer: differs from the UTC projection", s != _provider_dt_str(summer_start))
assert_covers("summer", summer_start, summer_end)

winter_start = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
winter_end = datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc)
s, e, ctx = vehicle_events_wire_window(winter_start, winter_end)
check(
    "winter (CET, UTC+1): window is shifted forward by one hour",
    (s, e) == ("2026-01-15 15:30:00", "2026-01-15 16:30:00"),
    f"got {s!r}..{e!r}",
)
check("winter: no DST widening", ctx["dst_widened_seconds"] == 0)
assert_covers("winter", winter_start, winter_end)

check(
    "naive input is read as UTC, not as host local time",
    vehicle_events_wire_window(
        summer_start.replace(tzinfo=None), summer_end.replace(tzinfo=None),
    )[:2] == (s0 := ("2026-07-21 16:30:00", "2026-07-21 17:30:00")),
    f"expected {s0}",
)


# ---------------------------------------------------------------------------
# 4. DST. Spring gap, autumn fold, and every shape the fold can take.
# ---------------------------------------------------------------------------
print("\n-- spring forward (2026-03-29 01:00 UTC) --")

spring_start = SPRING_FORWARD_UTC - timedelta(hours=2)
spring_end = SPRING_FORWARD_UTC + timedelta(hours=2)
s, e, ctx = vehicle_events_wire_window(spring_start, spring_end)
check(
    "spring: window spanning the gap is exact and unwidened",
    (s, e) == ("2026-03-29 00:00:00", "2026-03-29 05:00:00")
    and ctx["dst_widened_seconds"] == 0,
    f"got {s!r}..{e!r} widened={ctx['dst_widened_seconds']}s",
)
assert_covers("spring: spans the gap", spring_start, spring_end)

for offset_minutes in range(-180, 181, 1):
    probe = SPRING_FORWARD_UTC + timedelta(minutes=offset_minutes)
    wall = parse_wire(vehicle_events_wire_window(probe, probe)[0])
    if 2 <= wall.hour < 3 and wall.date() == SPRING_FORWARD_UTC.date():
        check("spring: serialization never emits a time inside the gap", False, str(wall))
        break
else:
    check("spring: serialization never emits a time inside the gap", True)

print("\n-- autumn fallback (2026-10-25 01:00 UTC) --")

# Warsaw local 02:00–02:59:59 occurs twice: first over 00:00–01:00 UTC (CEST),
# then over 01:00–02:00 UTC (CET). The whole ambiguous absolute region is the
# two-hour span 00:00–02:00 UTC.
fold_first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
fold_second = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
check(
    "autumn: two distinct instants serialize to the same wire string",
    _wall_clock_wire_dt_str(fold_first)
    == _wall_clock_wire_dt_str(fold_second)
    == "2026-10-25 02:30:00",
)

autumn_cases = {
    "spans the whole fold": (
        AUTUMN_FALLBACK_UTC - timedelta(hours=2),
        AUTUMN_FALLBACK_UTC + timedelta(hours=2),
    ),
    "entirely inside the first occurrence": (
        datetime(2026, 10, 25, 0, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc),
    ),
    "entirely inside the second occurrence": (
        datetime(2026, 10, 25, 1, 10, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 1, 50, tzinfo=timezone.utc),
    ),
    "exactly the first occurrence": (
        datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc),
    ),
    "start inside the fold, end after it": (
        datetime(2026, 10, 25, 0, 40, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc),
    ),
    "start before the fold, end inside it": (
        datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 1, 20, tzinfo=timezone.utc),
    ),
    "short remainder chunk inside the fold": (
        datetime(2026, 10, 25, 1, 44, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 1, 45, tzinfo=timezone.utc),
    ),
    "zero-length instant inside the fold": (
        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
    ),
    "a 30-minute chunk at the minimum production size": (
        datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc),
    ),
    "a 4-hour chunk at the default production size": (
        datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc),
    ),
}

for label, (a, b) in autumn_cases.items():
    ws, we = assert_covers(f"autumn: {label}", a, b)
    check(
        f"autumn: {label} does not serialize to an inverted interval",
        parse_wire(ws) <= parse_wire(we),
        f"wire={ws!r}..{we!r}",
    )

inside_ctx = vehicle_events_wire_window(
    datetime(2026, 10, 25, 0, 10, tzinfo=timezone.utc),
    datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc),
)[2]
check(
    "autumn: a window wholly inside one occurrence is widened, not left ambiguous",
    inside_ctx["dst_widened_seconds"] > 0,
    f"widened {inside_ctx['dst_widened_seconds']}s",
)
check(
    "autumn: only the endpoint that is ambiguous in the unsafe direction is widened",
    # The start's latest reading (01:10Z) is after the intended start, so it
    # must move back one hour. The end's earliest reading (00:50Z) already
    # satisfies the invariant, so widening it would be gratuitous over-fetch.
    inside_ctx["dst_widened_start_seconds"] == 3600
    and inside_ctx["dst_widened_end_seconds"] == 0,
    f"ctx={inside_ctx!r}",
)


# ---------------------------------------------------------------------------
# 5. Exhaustive sweep, plus the window-limit invariants that `/trips` does not
#    have. Every shape a production event chunker can produce near either
#    transition, on a one-minute grid.
# ---------------------------------------------------------------------------
print("\n-- exhaustive DST sweep and window limits --")

# 30 min is the production minimum chunk, 4 h the default; the rest bracket
# adaptive halving and the largest window the provider boundary accepts.
sweep_lengths = [
    timedelta(seconds=1),
    timedelta(minutes=1),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=4),
    timedelta(hours=12),
    VEHICLE_EVENTS_MAX_INTENDED_WINDOW,
]

sweep_total = 0
sweep_under_fetch = 0
worst_effective = timedelta(0)
worst_wall_clock = timedelta(0)
worst_widening = timedelta(0)

for transition in (SPRING_FORWARD_UTC, AUTUMN_FALLBACK_UTC):
    for offset_minutes in range(-180, 181):
        base = transition + timedelta(minutes=offset_minutes)
        for length in sweep_lengths:
            a, b = base, base + length
            ws, we, wctx = vehicle_events_wire_window(a, b)
            sweep_total += 1

            rs, re_ = readings(ws), readings(we)
            if not rs or not re_ or max(rs) > a or min(re_) < b:
                sweep_under_fetch += 1

            effective = timedelta(seconds=wctx["effective_window_seconds"])
            wall_clock = timedelta(seconds=wctx["wire_wall_clock_span_seconds"])
            worst_effective = max(worst_effective, effective)
            worst_wall_clock = max(worst_wall_clock, wall_clock)
            worst_widening = max(worst_widening, timedelta(seconds=wctx["dst_widened_seconds"]))

check(
    f"exhaustive: no under-fetch in {sweep_total} DST-adjacent event window shapes",
    sweep_under_fetch == 0,
    f"{sweep_under_fetch} under-fetching shapes",
)
check(
    "exhaustive: widening never exceeds one Warsaw offset change per side",
    worst_widening <= timedelta(hours=2),
    f"worst widening {worst_widening}",
)
check(
    "exhaustive: post-widen absolute span stays below the provider maximum",
    worst_effective < VEHICLE_EVENTS_MAX_REQUEST_WINDOW,
    f"worst effective span {worst_effective} vs limit {VEHICLE_EVENTS_MAX_REQUEST_WINDOW}",
)
check(
    "exhaustive: wire wall-clock span stays below the provider maximum",
    worst_wall_clock < VEHICLE_EVENTS_MAX_REQUEST_WINDOW,
    f"worst wall-clock span {worst_wall_clock} vs limit {VEHICLE_EVENTS_MAX_REQUEST_WINDOW}",
)

# The caller-side bound exists so an oversized window is split before
# conversion rather than rejected by the provider mid-run.
check(
    "the intended-window bound leaves room for worst-case widening",
    VEHICLE_EVENTS_MAX_INTENDED_WINDOW + timedelta(hours=2) < VEHICLE_EVENTS_MAX_REQUEST_WINDOW,
)
try:
    vehicle_events_wire_window(
        summer_start, summer_start + VEHICLE_EVENTS_MAX_INTENDED_WINDOW + timedelta(seconds=1),
    )
    check("an oversized intended window is rejected before conversion", False)
except ValueError as exc:
    check(
        "an oversized intended window is rejected before conversion",
        "intended window" in str(exc),
        str(exc),
    )
check(
    "a window exactly at the intended bound is accepted",
    bool(vehicle_events_wire_window(
        summer_start, summer_start + VEHICLE_EVENTS_MAX_INTENDED_WINDOW,
    )[0]),
)

# Adjacent chunks must tile the range: overlap is acceptable, a hole is not.
print("\n-- chunk adjacency --")

for label, (range_start, chunk) in {
    "autumn, 4h chunks": (AUTUMN_FALLBACK_UTC - timedelta(hours=8), timedelta(hours=4)),
    "autumn, 30m chunks": (AUTUMN_FALLBACK_UTC - timedelta(hours=3), timedelta(minutes=30)),
    "spring, 4h chunks": (SPRING_FORWARD_UTC - timedelta(hours=8), timedelta(hours=4)),
}.items():
    range_end = range_start + timedelta(hours=16)
    holes = 0
    previous_end: datetime | None = None
    current = range_start
    while current < range_end:
        chunk_end = min(current + chunk, range_end)
        # Mirrors `_iter_vehicle_events_fleet_request_windows`: a non-final
        # chunk asks up to one second before the next chunk's start.
        request_end = chunk_end if chunk_end >= range_end else chunk_end - timedelta(seconds=1)
        if request_end > current:
            _ws, _we, wctx = vehicle_events_wire_window(current, request_end)
            effective_start = datetime.fromisoformat(wctx["effective_window_start_utc"])
            effective_end = datetime.fromisoformat(wctx["effective_window_end_utc"])
            if previous_end is not None and effective_start > previous_end + timedelta(seconds=1):
                holes += 1
            previous_end = max(previous_end or effective_end, effective_end)
        current = chunk_end
    check(f"adjacent chunks leave no hole ({label})", holes == 0, f"{holes} holes")


# ---------------------------------------------------------------------------
# 6. Response side and downstream matching are unchanged: `event_ts` is UTC and
#    is never reinterpreted as Warsaw. A double conversion would show up here.
# ---------------------------------------------------------------------------
print("\n-- response parsing stays UTC --")

check(
    "a naive response event_ts is read as UTC, not as Warsaw",
    _normalize_vehicle_event({"event_ts": "2026-06-29 08:00:33"})["event_ts"]
    == datetime(2026, 6, 29, 8, 0, 33, tzinfo=timezone.utc),
)
check(
    "an offset-carrying response event_ts is normalized to UTC",
    _normalize_vehicle_event({"event_ts": "2026-06-29 10:00:33+02:00"})["event_ts"]
    == datetime(2026, 6, 29, 8, 0, 33, tzinfo=timezone.utc),
)
check(
    "the fetched row's event_ts is untouched by the request-side conversion",
    fleet_rows[0]["event_ts"] == datetime(2026, 6, 29, 8, 0, 33, tzinfo=timezone.utc)
    and reg_rows[0]["event_ts"] == datetime(2026, 6, 29, 8, 0, 33, tzinfo=timezone.utc),
    f"fleet={fleet_rows!r}",
)
check(
    "the live-probe trip still contains the live-probe event by absolute matching",
    # trip 432316079: 08:00:32Z – 08:43:57Z, matcher is start <= event <= end.
    datetime(2026, 6, 29, 8, 0, 32, tzinfo=timezone.utc)
    <= fleet_rows[0]["event_ts"]
    <= datetime(2026, 6, 29, 8, 43, 57, tzinfo=timezone.utc),
)


# ---------------------------------------------------------------------------
# 7. Failure behaviour: the conversion must not turn a failure into an apparent
#    success, and must not swallow a page cap.
# ---------------------------------------------------------------------------
print("\n-- failure behaviour --")

bad_client, _bad_fake = build_client({1: {"meta": {"current_page": 1, "last_page": 1}}})
try:
    bad_client.fetch_vehicle_events_fleet(
        start_timestamp=PROBE_START,
        end_timestamp=PROBE_END,
        sub_window_label="malformed",
        limit=1000,
        max_pages=500,
    )
    check("a malformed response still raises rather than returning rows", False)
except Exception as exc:  # TelematicsProviderSafetyError
    check(
        "a malformed response still raises rather than returning rows",
        getattr(exc, "code", None) == "MALFORMED_RESPONSE",
        repr(exc),
    )

capped_client, capped_fake = build_client({
    1: {"data": [{"event_id": 1}], "meta": {"current_page": 1, "last_page": 5}},
    2: {"data": [{"event_id": 2}], "meta": {"current_page": 2, "last_page": 5}},
})
_rows, stats = capped_client.fetch_vehicle_events_fleet(
    start_timestamp=PROBE_START,
    end_timestamp=PROBE_END,
    sub_window_label="capped",
    limit=1000,
    max_pages=2,
    return_stats=True,
)
check(
    "an incomplete page walk is still reported as stopped_by_max_pages",
    stats.get("stopped_by_max_pages") is True and len(capped_fake.calls) == 2,
    f"stats={stats!r}",
)

try:
    vehicle_events_wire_window(PROBE_END, PROBE_START)
    check("an inverted intended window is not silently accepted", False)
except ValueError:
    check("an inverted intended window is not silently accepted", True)


# ---------------------------------------------------------------------------
# 8. Blast radius: endpoints whose contract has not been measured must keep
#    their previous serialization.
# ---------------------------------------------------------------------------
print("\n-- unrelated endpoints unchanged --")

check(
    "_provider_dt_str still emits the UTC projection",
    _provider_dt_str(summer_start) == "2026-07-21 14:30:00",
)

notif_client, notif_fake = build_client({
    1: {"data": [], "meta": {"current_page": 1, "last_page": 1}},
})
notif_client.fetch_notifications(
    window_start_ts=summer_start,
    window_end_ts=summer_end,
)
notif_params = notif_fake.calls[0]["params"]
check(
    "/alerts/notifications still serializes its window as UTC",
    notif_params.get("filter[date_from]") == "2026-07-21 14:30:00"
    and notif_params.get("filter[date_to]") == "2026-07-21 15:30:00",
    f"params={notif_params!r}",
)


print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED:")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("ALL /vehicles/events WIRE-TIME CONTRACT CHECKS PASSED")
