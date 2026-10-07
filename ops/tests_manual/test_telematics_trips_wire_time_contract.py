#!/usr/bin/env python3
"""Regression suite for the Telematics `/trips` request wire-time contract.

Pure: stdlib only, no network, no database, no secrets, no host-clock
dependence. `zoneinfo` is used to build Europe/Warsaw fixtures and to reason
about DST transitions.

The contract under test (established by live GET-only probes on 2026-08-10):

  * `/trips` request `start_timestamp` / `end_timestamp` are Europe/Warsaw
    local wall-clock;
  * `/trips` response row timestamps are UTC.

The autumn fallback case is the load-bearing one. Because the provider
addresses trips by wall-clock only, a repeated local hour is ambiguous. The
suite proves that under the *worst-case* provider resolution of every ambiguous
endpoint, the requested interval is still a superset of the intended UTC
interval — i.e. no UTC interval can be skipped.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_trips_wire_time_contract.py
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
    TRIPS_MAX_SUB_WINDOW_DAYS,
    TRIPS_REQUEST_TIMEZONE_NAME,
    _provider_dt_str,
    _trips_wire_dt_str,
    iter_31d_windows,
    trips_wire_window,
)
from jobs.api.telematics.sync_trips_and_speeding import _parse_provider_dt  # noqa: E402

WARSAW = ZoneInfo(TRIPS_REQUEST_TIMEZONE_NAME)
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
        print(f"FAIL  {name}" + (f"  --  {detail}" if detail else ""))


def parse_wire(text: str) -> datetime:
    return datetime.strptime(text, WIRE_FMT)


def utc_candidates(wall: datetime) -> list[datetime]:
    """Every UTC instant whose Warsaw wall-clock equals `wall`.

    Returns two candidates inside the autumn fold, one normally, and none
    inside the spring gap (`astimezone` can never produce such a wall-clock,
    so this is only used as an assertion helper).
    """
    out: list[datetime] = []
    for fold in (0, 1):
        aware = wall.replace(tzinfo=WARSAW, fold=fold)
        as_utc = aware.astimezone(timezone.utc)
        if as_utc.astimezone(WARSAW).replace(tzinfo=None) == wall and as_utc not in out:
            out.append(as_utc)
    return sorted(out)


# ---------------------------------------------------------------------------
# 1. Summer — the case the live probe validated byte for byte.
# ---------------------------------------------------------------------------

summer_start = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)
summer_end = datetime(2026, 7, 21, 15, 30, tzinfo=timezone.utc)
s, e, ctx = trips_wire_window(summer_start, summer_end)
check(
    "summer: wire window is Warsaw wall-clock (UTC+02)",
    (s, e) == ("2026-07-21 16:30:00", "2026-07-21 17:30:00"),
    f"got {s!r}..{e!r}",
)
check("summer: no DST widening", ctx["dst_widened_seconds"] == 0)
check(
    "summer: reproduces the request the 2026-08-10 live probe succeeded with",
    s == "2026-07-21 16:30:00",
)
check(
    "summer: wire encoding differs from the previous UTC serialization",
    s != _provider_dt_str(summer_start),
    "the bug would be invisible if these agreed",
)
# Round trip: a UTC row timestamp resolves back to the intended instant.
row_instant = _parse_provider_dt("2026-07-21 14:30:19")
check(
    "summer: response row parses as UTC and lands inside the intended window",
    row_instant is not None and summer_start <= row_instant <= summer_end,
    f"got {row_instant}",
)
check(
    "summer: that same row is 16:30:19 Warsaw, matching the wire frame",
    row_instant.astimezone(WARSAW).strftime(WIRE_FMT) == "2026-07-21 16:30:19",
)

# ---------------------------------------------------------------------------
# 2. Winter — UTC+01.
# ---------------------------------------------------------------------------

winter_start = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
winter_end = datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc)
s, e, ctx = trips_wire_window(winter_start, winter_end)
check(
    "winter: wire window is Warsaw wall-clock (UTC+01)",
    (s, e) == ("2026-01-15 15:30:00", "2026-01-15 16:30:00"),
    f"got {s!r}..{e!r}",
)
check("winter: no DST widening", ctx["dst_widened_seconds"] == 0)

# ---------------------------------------------------------------------------
# 3. Host-timezone independence and idempotence.
# ---------------------------------------------------------------------------

naive_utc = datetime(2026, 7, 21, 14, 30)
check(
    "naive input is read as UTC, never as host-local time",
    _trips_wire_dt_str(naive_utc) == _trips_wire_dt_str(summer_start),
)
check(
    "output is a string, so the conversion cannot be applied twice",
    isinstance(_trips_wire_dt_str(summer_start), str),
)

# ---------------------------------------------------------------------------
# 4. Spring forward — the missing local hour.
# ---------------------------------------------------------------------------

spring_start = SPRING_FORWARD_UTC - timedelta(minutes=30)
spring_end = SPRING_FORWARD_UTC + timedelta(minutes=30)
s, e, ctx = trips_wire_window(spring_start, spring_end)
# Local time advances monotonically across the gap and neither endpoint is
# ambiguous, so the spring window is already exact — widening it would be
# over-fetch with no correctness value.
check(
    "spring: an unambiguous window spanning the gap needs no widening",
    ctx["dst_widened_seconds"] == 0,
    f"widened {ctx['dst_widened_seconds']}s",
)
check(
    "spring: worst-case reading still covers the intended interval",
    max(utc_candidates(parse_wire(s))) <= spring_start
    and min(utc_candidates(parse_wire(e))) >= spring_end,
)
check(
    "spring: wire window is well ordered, never inverted",
    parse_wire(s) < parse_wire(e),
    f"{s!r}..{e!r}",
)
check(
    "spring: wire window brackets the transition",
    parse_wire(s) <= datetime(2026, 3, 29, 1, 59, 59) and parse_wire(e) >= datetime(2026, 3, 29, 3, 0, 0),
    f"got {s!r}..{e!r}",
)
# No emitted wall-clock may fall inside the non-existent local hour.
gap_free = True
probe = spring_start
while probe <= spring_end:
    wall = parse_wire(_trips_wire_dt_str(probe))
    if datetime(2026, 3, 29, 2, 0, 0) <= wall < datetime(2026, 3, 29, 3, 0, 0):
        gap_free = False
        break
    probe += timedelta(minutes=1)
check("spring: no request timestamp is emitted inside the non-existent hour", gap_free)

# ---------------------------------------------------------------------------
# 5. Autumn fallback — the repeated local hour. The critical case.
# ---------------------------------------------------------------------------

fold_first = AUTUMN_FALLBACK_UTC - timedelta(hours=1)   # local 02:00, first pass
fold_second = AUTUMN_FALLBACK_UTC                        # local 02:00, second pass
check(
    "autumn: the fold really is ambiguous in this fixture",
    _trips_wire_dt_str(fold_first) == _trips_wire_dt_str(fold_second) == "2026-10-25 02:00:00",
)

# A window contained entirely inside the fold is the pathological case: without
# widening it serializes to an inverted or empty wall-clock interval.
inside_start = AUTUMN_FALLBACK_UTC - timedelta(minutes=30)
inside_end = AUTUMN_FALLBACK_UTC + timedelta(minutes=30)
naive_s = _trips_wire_dt_str(inside_start)
naive_e = _trips_wire_dt_str(inside_end)
check(
    "autumn: unwidened serialization would be inverted or degenerate",
    parse_wire(naive_e) <= parse_wire(naive_s),
    f"unwidened {naive_s!r}..{naive_e!r}",
)

s, e, ctx = trips_wire_window(inside_start, inside_end)
check("autumn: both ambiguous endpoints are widened", ctx["dst_widened_seconds"] == 7200)
check("autumn: widened wire window is well ordered", parse_wire(s) < parse_wire(e), f"{s!r}..{e!r}")

# The rigorous no-skip proof. The provider may resolve each ambiguous wire
# endpoint to either occurrence. The worst case for coverage is the LATEST
# possible start and the EARLIEST possible end.
worst_start = max(utc_candidates(parse_wire(s)))
worst_end = min(utc_candidates(parse_wire(e)))
check(
    "autumn: worst-case provider resolution still covers the intended interval",
    worst_start <= inside_start and worst_end >= inside_end,
    f"worst case [{worst_start}, {worst_end}] vs intended [{inside_start}, {inside_end}]",
)
check(
    "autumn: over-fetch, never under-fetch",
    worst_start <= inside_start and worst_end >= inside_end,
)

# Every UTC instant of the intended window is addressed by the wire window
# under either resolution, sampled minute by minute across the whole fold.
skipped = []
probe = AUTUMN_FALLBACK_UTC - timedelta(hours=2)
window_start = AUTUMN_FALLBACK_UTC - timedelta(hours=2)
window_end = AUTUMN_FALLBACK_UTC + timedelta(hours=2)
ws, we, _ = trips_wire_window(window_start, window_end)
ws_worst = max(utc_candidates(parse_wire(ws)))
we_worst = min(utc_candidates(parse_wire(we)))
while probe <= window_end:
    if not (ws_worst <= probe <= we_worst):
        skipped.append(probe)
    probe += timedelta(minutes=1)
check(
    "autumn: no UTC minute of a fold-spanning window is skipped",
    not skipped,
    f"{len(skipped)} skipped instants, first {skipped[0] if skipped else None}",
)

# A realistic rolling window that merely contains the transition.
roll_start = AUTUMN_FALLBACK_UTC - timedelta(days=14)
roll_end = AUTUMN_FALLBACK_UTC + timedelta(days=1)
s, e, ctx = trips_wire_window(roll_start, roll_end)
check(
    "autumn: a 15-day rolling window containing the fold is not truncated",
    max(utc_candidates(parse_wire(s))) <= roll_start
    and min(utc_candidates(parse_wire(e))) >= roll_end,
)

# ---------------------------------------------------------------------------
# 5b. Regression: a window lying WHOLLY INSIDE one occurrence of the repeated
# hour. Both endpoints then carry the same UTC offset, so a widening rule
# triggered by an offset difference does not fire — yet both wire strings are
# ambiguous, and the provider may resolve them to the other occurrence. Before
# the fix this serialized to an inverted interval and could omit the intended
# window entirely. Reachable in production: `_build_trip_fetch_chunks` emits a
# short remainder chunk whenever the effective window is not a whole multiple
# of `chunk_days`, and the insert-only backfill accepts arbitrary operator
# windows.
# ---------------------------------------------------------------------------

fold_contained = [
    ("first occurrence", AUTUMN_FALLBACK_UTC - timedelta(minutes=50), AUTUMN_FALLBACK_UTC - timedelta(minutes=10)),
    ("second occurrence", AUTUMN_FALLBACK_UTC + timedelta(minutes=10), AUTUMN_FALLBACK_UTC + timedelta(minutes=50)),
    ("5-minute tail, first occurrence", AUTUMN_FALLBACK_UTC - timedelta(minutes=35), AUTUMN_FALLBACK_UTC - timedelta(minutes=30)),
    ("5-minute tail, second occurrence", AUTUMN_FALLBACK_UTC + timedelta(minutes=25), AUTUMN_FALLBACK_UTC + timedelta(minutes=30)),
    ("exactly the first occurrence", AUTUMN_FALLBACK_UTC - timedelta(hours=1), AUTUMN_FALLBACK_UTC),
    ("zero-length inside the fold", AUTUMN_FALLBACK_UTC - timedelta(minutes=30), AUTUMN_FALLBACK_UTC - timedelta(minutes=30)),
]
for label, fc_start, fc_end in fold_contained:
    fs, fe, fctx = trips_wire_window(fc_start, fc_end)
    check(
        f"autumn: window inside the fold ({label}) is never under-fetched",
        max(utc_candidates(parse_wire(fs))) <= fc_start
        and min(utc_candidates(parse_wire(fe))) >= fc_end,
        f"wire {fs!r}..{fe!r} vs intended [{fc_start}, {fc_end}]",
    )
    check(
        f"autumn: window inside the fold ({label}) is not inverted",
        parse_wire(fs) <= parse_wire(fe),
        f"wire {fs!r}..{fe!r}",
    )

# ---------------------------------------------------------------------------
# 5c. Exhaustive invariant sweep. Every window shape a chunk, sub-window or
# operator recovery can produce, on a one-minute grid across both 2026
# transitions. The invariant is stated on the worst-case provider reading, not
# on endpoint offsets:
#
#     max(readings(start_str)) <= start   and   min(readings(end_str)) >= end
# ---------------------------------------------------------------------------

sweep_failures = []
sweep_total = 0
for transition in (SPRING_FORWARD_UTC, AUTUMN_FALLBACK_UTC):
    for minute_offset in range(-180, 181):
        sw_start = transition + timedelta(minutes=minute_offset)
        for length_s in (1, 5, 60, 299, 3600, 3601, 7200, 86_400, 2 * 86_400, 14 * 86_400, 30 * 86_400):
            sw_end = sw_start + timedelta(seconds=length_s)
            sweep_total += 1
            a, b, _ = trips_wire_window(sw_start, sw_end)
            if not (max(utc_candidates(parse_wire(a))) <= sw_start
                    and min(utc_candidates(parse_wire(b))) >= sw_end):
                sweep_failures.append((sw_start, sw_end, a, b))
check(
    f"exhaustive: no under-fetch in {sweep_total} DST-adjacent window shapes",
    not sweep_failures,
    f"{len(sweep_failures)} failures, first {sweep_failures[0] if sweep_failures else None}",
)

# Sub-second inputs must not shrink the requested interval.
sub_s, sub_e, _ = trips_wire_window(
    datetime(2026, 7, 21, 14, 30, 0, 500_000, tzinfo=timezone.utc),
    datetime(2026, 7, 21, 15, 30, 0, 500_000, tzinfo=timezone.utc),
)
check(
    "sub-second bounds are floored at the start and ceiled at the end",
    (sub_s, sub_e) == ("2026-07-21 16:30:00", "2026-07-21 17:30:01"),
    f"got {sub_s!r}..{sub_e!r}",
)

# ---------------------------------------------------------------------------
# 6. Sub-window splitting stays inside the provider's 31-day maximum.
# ---------------------------------------------------------------------------

check("trips sub-windows split at 30 days", TRIPS_MAX_SUB_WINDOW_DAYS == 30)

long_start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
long_end = long_start + timedelta(days=75)
too_long = []
for sub_start, sub_end in iter_31d_windows(long_start, long_end, max_days=TRIPS_MAX_SUB_WINDOW_DAYS):
    ws, we, _ = trips_wire_window(sub_start, sub_end)
    span = parse_wire(we) - parse_wire(ws)
    if span > timedelta(days=31):
        too_long.append((ws, we, span))
check(
    "widened sub-windows never exceed the provider's 31-day limit",
    not too_long,
    f"{too_long[:1]}",
)

# The split must still tile the whole requested range without a hole.
subs = list(iter_31d_windows(long_start, long_end, max_days=TRIPS_MAX_SUB_WINDOW_DAYS))
tiled = subs[0][0] <= long_start and subs[-1][1] >= long_end and all(
    subs[i + 1][0] <= subs[i][1] for i in range(len(subs) - 1)
)
check("sub-window split tiles the requested range with no hole", tiled)

# ---------------------------------------------------------------------------
# 7. Blast radius — other endpoints keep the previous serialization.
# ---------------------------------------------------------------------------

check(
    "_provider_dt_str is unchanged for every non-/trips endpoint",
    _provider_dt_str(summer_start) == "2026-07-21 14:30:00",
)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL WIRE-TIME CONTRACT CHECKS PASSED")
