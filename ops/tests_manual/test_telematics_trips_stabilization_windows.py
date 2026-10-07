#!/usr/bin/env python3
"""Focused C4 tests for pure stabilized Telematics schedule-window derivation.

Pure: stdlib only, no network, no database, no secrets, no clock. `zoneinfo` is
used here only to build representative UTC fire fixtures the way
`dispatcher.latest_scheduled_fire_local` would; the helper under test never
touches a timezone database.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_trips_stabilization_windows.py
"""
from __future__ import annotations

import dataclasses
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import coverage_windows  # noqa: E402
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    EffectiveWindow,
    derive_effective_window,
)

UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
SECOND = timedelta(seconds=1)
DAY = 86_400

D_DEFAULT = 10_800
O_DEFAULT = 3_600
R_DEFAULT = 2_678_400

# Far enough back that no derived window ever begins before it, so the coverage
# lower bound is never the reason a case passes.
A_FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _derive(
    *,
    fire: datetime,
    lookback_days: object = 1,
    delay: object = D_DEFAULT,
    overlap: object = O_DEFAULT,
    recovery: object = R_DEFAULT,
    coverage_start: object = A_FAR_PAST,
    covered_through: object | None = None,
) -> EffectiveWindow:
    """Derive with `W = F` unless overridden.

    `W = F` guarantees `W - O >= base` for every non-negative `L` and `D`, so
    `min(base, W - O)` selects `base`. That isolates pure architecture-A
    arithmetic (`docs/13_…` §4) from the coverage-expansion branch of §5.2.
    """
    return derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=lookback_days,
        stabilization_delay_seconds=delay,
        overlap_seconds=overlap,
        max_recovery_span_seconds=recovery,
        coverage_start_ts=coverage_start,
        covered_through_ts=fire if covered_through is None else covered_through,
    )


def _expect_value_error(callable_, label: str) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError(f"invalid input was accepted: {label}")


def _capped_case(
    *,
    fire: datetime,
    grid_steps_after_w: int,
    lookback_days: int = 1,
    delay: int = D_DEFAULT,
    overlap: int = 0,
    recovery: int = 86_400,
) -> EffectiveWindow:
    """Place `W` so the recovery cap pins `E_start` exactly N seconds after it.

    Because `candidate_start = min(base, W − O)` is never later than `W`, the
    only way `E_start` can land at or after `W` at all is the `max(…, E_end − R)`
    recovery cap. These are therefore the only shapes that can exercise the
    connectivity boundary, which is itself a fact worth pinning.
    """
    covered_through = (
        fire
        - timedelta(seconds=delay)
        - timedelta(seconds=recovery)
        - timedelta(seconds=grid_steps_after_w)
    )
    result = _derive(
        fire=fire,
        lookback_days=lookback_days,
        delay=delay,
        overlap=overlap,
        recovery=recovery,
        covered_through=covered_through,
    )
    assert result.effective_window_start_ts == (
        result.effective_window_end_ts - timedelta(seconds=recovery)
    ), "fixture did not exercise the recovery cap"
    assert result.effective_window_start_ts == (
        result.covered_through_ts + timedelta(seconds=grid_steps_after_w)
    )
    return result


def _projected_covered_through(window: EffectiveWindow) -> datetime:
    """`max(W, E_end)` computed by the *test*, never by C4.

    C4 does not expose, compute or persist coverage advancement; that is C6.
    Recomputing it here proves the value C6 would obtain is available from a C4
    result without C4 asserting it.
    """
    return max(window.covered_through_ts, window.effective_window_end_ts)


# ---------------------------------------------------------------------------
# 1. Nominal arithmetic and the input contract
# ---------------------------------------------------------------------------

def test_nominal_arithmetic() -> None:
    daily = _derive(fire=_utc(2026, 5, 12, 2, 0, 0), lookback_days=1)
    assert daily.scheduled_fire_ts == _utc(2026, 5, 12, 2, 0, 0)
    assert daily.nominal_window_end_ts == _utc(2026, 5, 12, 2, 0, 0)
    assert daily.nominal_window_start_ts == _utc(2026, 5, 11, 2, 0, 0)
    assert (
        daily.nominal_window_end_ts - daily.nominal_window_start_ts
    ) == timedelta(seconds=DAY)

    weekly = _derive(fire=_utc(2026, 5, 11, 2, 0, 0), lookback_days=7)
    assert weekly.nominal_window_start_ts == _utc(2026, 5, 4, 2, 0, 0)
    assert (
        weekly.nominal_window_end_ts - weekly.nominal_window_start_ts
    ) == timedelta(seconds=7 * DAY)

    # Zero lookback: a degenerate nominal window with a shared endpoint. The
    # dispatcher's schedule column permits `lookback_days = 0`.
    zero = _derive(fire=_utc(2026, 5, 12, 2, 0, 0), lookback_days=0)
    assert zero.nominal_window_start_ts == zero.nominal_window_end_ts
    assert zero.effective_window_start_ts == zero.effective_window_end_ts - timedelta(
        seconds=O_DEFAULT
    )

    # Exact second precision is preserved end to end.
    odd = _derive(fire=_utc(2026, 5, 12, 2, 0, 7), lookback_days=1)
    assert odd.effective_window_end_ts == _utc(2026, 5, 12, 2, 0, 7) - timedelta(
        seconds=D_DEFAULT
    )
    assert odd.effective_window_start_ts == _utc(2026, 5, 11, 2, 0, 7) - timedelta(
        seconds=D_DEFAULT + O_DEFAULT
    )

    # A zero-offset tzinfo other than `timezone.utc` is the same instant and is
    # normalized to canonical UTC, exactly as the dispatcher's `_iso_z` does.
    aliased = _derive(
        fire=datetime(2026, 5, 12, 2, 0, 0, tzinfo=ZoneInfo("UTC")), lookback_days=1
    )
    assert aliased.scheduled_fire_ts.tzinfo is UTC
    assert aliased.scheduled_fire_ts == _utc(2026, 5, 12, 2, 0, 0)


def test_input_contract_rejections() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)

    # Naive datetimes are rejected on every timestamp input.
    _expect_value_error(
        lambda: _derive(fire=datetime(2026, 5, 12, 2, 0, 0)), "naive fire"
    )
    _expect_value_error(
        lambda: _derive(fire=fire, coverage_start=datetime(2026, 1, 1)),
        "naive coverage_start_ts",
    )
    _expect_value_error(
        lambda: _derive(fire=fire, covered_through=datetime(2026, 5, 12)),
        "naive covered_through_ts",
    )

    # Non-UTC offsets are rejected rather than silently converted.
    for offset_hours in (1, 2, -5):
        tz = timezone(timedelta(hours=offset_hours))
        _expect_value_error(
            lambda t=tz: _derive(fire=datetime(2026, 5, 12, 2, 0, 0, tzinfo=t)),
            f"fire with offset {offset_hours}h",
        )
    _expect_value_error(
        lambda: _derive(
            fire=fire, covered_through=datetime(2026, 5, 12, 2, tzinfo=WARSAW)
        ),
        "covered_through_ts in Europe/Warsaw (non-zero offset)",
    )

    # Non-zero microseconds are rejected: the scheduled path is a one-second
    # grid and this module must not invent a rounding contract.
    _expect_value_error(
        lambda: _derive(fire=datetime(2026, 5, 12, 2, 0, 0, 1, tzinfo=UTC)),
        "fire with microseconds",
    )
    _expect_value_error(
        lambda: _derive(
            fire=fire,
            covered_through=datetime(2026, 5, 12, 1, 0, 0, 500_000, tzinfo=UTC),
        ),
        "covered_through_ts with microseconds",
    )
    _expect_value_error(
        lambda: _derive(
            fire=fire,
            coverage_start=datetime(2026, 1, 1, 0, 0, 0, 999_999, tzinfo=UTC),
        ),
        "coverage_start_ts with microseconds",
    )

    # Non-datetime timestamp inputs, including dates and ISO strings.
    for bad in ("2026-05-12T02:00:00Z", date(2026, 5, 12), 1_777_000_000, None, True):
        _expect_value_error(lambda b=bad: _derive(fire=b), f"fire={bad!r}")

    # Malformed durations. Booleans must not pass as integers.
    for bad in ("10800", 10_800.0, True, False, -1, None):
        _expect_value_error(lambda b=bad: _derive(fire=fire, delay=b), f"D={bad!r}")
    for bad in ("3600", 3_600.0, True, -1, None):
        _expect_value_error(lambda b=bad: _derive(fire=fire, overlap=b), f"O={bad!r}")
    # 2_768_401 is one second above the M7 ceiling (32 d + O, migration 072);
    # 2_678_401 is deliberately NOT here any more — it is a legal span since M7.
    for bad in ("2768400", 2_768_400.0, True, 0, -1, 2_768_401, None):
        _expect_value_error(lambda b=bad: _derive(fire=fire, recovery=b), f"R={bad!r}")
    for bad in ("1", 1.0, True, False, -1, None):
        _expect_value_error(
            lambda b=bad: _derive(fire=fire, lookback_days=b), f"lookback_days={bad!r}"
        )

    # Relationship constraints.
    _expect_value_error(
        lambda: _derive(fire=fire, overlap=3_601, recovery=3_600), "O > R"
    )
    _expect_value_error(
        lambda: _derive(
            fire=fire,
            coverage_start=_utc(2026, 5, 12, 0, 0, 1),
            covered_through=_utc(2026, 5, 12, 0, 0, 0),
        ),
        "A > W",
    )


# ---------------------------------------------------------------------------
# 2. Normal connected operation
# ---------------------------------------------------------------------------

def test_normal_connected_operation() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    base = fire - timedelta(seconds=DAY + D_DEFAULT + O_DEFAULT)
    e_end = fire - timedelta(seconds=D_DEFAULT)

    # W exactly at the previous effective end — the steady state of a
    # zero-slack daily schedule. `base` and `W - O` coincide, so the coverage
    # interval is arithmetically inert and the window is connected with `O`
    # seconds of overlap duration.
    previous_end = fire - timedelta(seconds=DAY + D_DEFAULT)
    near = _derive(fire=fire, covered_through=previous_end)
    assert near.effective_window_start_ts == previous_end - timedelta(
        seconds=O_DEFAULT
    )
    assert near.effective_window_start_ts == base
    assert near.is_connected is True

    # W one second behind that steady state: `W - O` now wins the `min` and the
    # request is widened backwards rather than shortened.
    lagging = _derive(fire=fire, covered_through=previous_end - SECOND)
    assert lagging.effective_window_start_ts == base - SECOND
    assert lagging.effective_window_start_ts < base
    assert lagging.is_connected is True

    # base earlier than W - O: `min` selects base and coverage is inert.
    inert = _derive(fire=fire, covered_through=fire)
    assert inert.effective_window_start_ts == base
    assert inert.effective_window_end_ts == e_end
    assert inert.is_connected is True

    # W - O earlier than base: `min` selects W - O.
    stale_w = base + timedelta(seconds=O_DEFAULT) - SECOND
    expansion = _derive(fire=fire, covered_through=stale_w)
    assert expansion.effective_window_start_ts == stale_w - timedelta(
        seconds=O_DEFAULT
    )
    assert expansion.is_connected is True

    # Below `W` the window can only widen, never detach: for every non-negative
    # O the pre-cap candidate is bounded above by W.
    for overlap in (0, 1, 3_600):
        bounded = _derive(fire=fire, covered_through=base, overlap=overlap)
        assert bounded.effective_window_start_ts <= bounded.covered_through_ts
        assert bounded.is_connected is True

    # E_start == W exactly (shared endpoint) is connected.
    shared = _capped_case(fire=fire, grid_steps_after_w=0)
    assert shared.effective_window_start_ts == shared.covered_through_ts
    assert shared.is_connected is True

    # E_start == W + 1 s exactly (adjacent on the grid) is connected.
    adjacent = _capped_case(fire=fire, grid_steps_after_w=1)
    assert adjacent.effective_window_start_ts == adjacent.covered_through_ts + SECOND
    assert adjacent.is_connected is True

    # E_start == W + 2 s leaves the single second W + 1 s uncovered and is
    # reported disconnected.
    detached = _capped_case(fire=fire, grid_steps_after_w=2)
    assert (
        detached.effective_window_start_ts == detached.covered_through_ts + 2 * SECOND
    )
    assert detached.is_connected is False

    # Overlapping effective window (E_start strictly before W).
    overlapping = _derive(fire=fire, covered_through=base + timedelta(hours=5))
    assert overlapping.effective_window_start_ts < overlapping.covered_through_ts
    assert overlapping.is_connected is True

    # Projected advancement, computed by the test only: new_W = E_end when
    # E_end is later than W, and remains W when it is not.
    forward = _derive(fire=fire, covered_through=base)
    assert _projected_covered_through(forward) == forward.effective_window_end_ts
    behind = _derive(fire=fire, covered_through=e_end + timedelta(hours=9))
    assert behind.effective_window_end_ts < behind.covered_through_ts
    assert _projected_covered_through(behind) == behind.covered_through_ts
    # Neither call changed the supplied bounds.
    assert forward.coverage_start_ts == A_FAR_PAST
    assert behind.covered_through_ts == e_end + timedelta(hours=9)


# ---------------------------------------------------------------------------
# 3. Recovery-span cap
# ---------------------------------------------------------------------------

def test_recovery_span_cap() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    e_end = fire - timedelta(seconds=D_DEFAULT)
    base = fire - timedelta(seconds=DAY + D_DEFAULT + O_DEFAULT)

    # Candidate start comfortably within R: the cap is inactive.
    within = _derive(fire=fire, covered_through=fire, recovery=R_DEFAULT)
    assert within.effective_window_start_ts == base
    assert within.effective_window_start_ts > e_end - timedelta(seconds=R_DEFAULT)

    # Candidate start earlier than E_end - R: the cap binds.
    ancient_w = e_end - timedelta(days=90)
    capped = _derive(fire=fire, covered_through=ancient_w, recovery=R_DEFAULT)
    assert capped.effective_window_start_ts == e_end - timedelta(seconds=R_DEFAULT)
    # A 90-day hole cannot be healed inside a 31-day cap: reported, not hidden.
    assert capped.is_connected is False

    # The cap can still produce a connected window when the hole fits.
    healable_w = e_end - timedelta(days=10)
    healed = _derive(fire=fire, covered_through=healable_w, recovery=R_DEFAULT)
    assert healed.effective_window_start_ts == healable_w - timedelta(
        seconds=O_DEFAULT
    )
    assert healed.effective_window_start_ts > e_end - timedelta(seconds=R_DEFAULT)
    assert healed.is_connected is True

    # Exact R activation boundary: at W = E_end - R + O the uncapped candidate
    # `W - O` lands precisely on `E_end - R`, so both branches agree.
    exact_r = R_DEFAULT
    boundary_w = e_end - timedelta(seconds=exact_r - O_DEFAULT)
    exact = _derive(fire=fire, covered_through=boundary_w, recovery=exact_r)
    assert exact.effective_window_start_ts == e_end - timedelta(seconds=exact_r)
    assert exact.effective_window_start_ts == boundary_w - timedelta(
        seconds=O_DEFAULT
    )
    assert exact.is_connected is True
    # One second earlier the cap binds — but the window is still connected,
    # because the cap has only consumed one second of the overlap pre-roll.
    just_past = _derive(
        fire=fire, covered_through=boundary_w - SECOND, recovery=exact_r
    )
    assert just_past.effective_window_start_ts == e_end - timedelta(seconds=exact_r)
    assert just_past.effective_window_start_ts > (
        just_past.covered_through_ts - timedelta(seconds=O_DEFAULT)
    )
    assert just_past.is_connected is True

    # The exact connectivity boundary produced by the cap: E_start == W + 1 s is
    # still connected, E_start == W + 2 s is not.
    assert _capped_case(fire=fire, grid_steps_after_w=1).is_connected is True
    assert _capped_case(fire=fire, grid_steps_after_w=2).is_connected is False

    # Reducing R can only move E_start later, so it exposes a gap; it can never
    # permit a jump over one.
    wide = _derive(fire=fire, covered_through=ancient_w, recovery=2_678_400)
    narrow = _derive(fire=fire, covered_through=ancient_w, recovery=86_400)
    assert narrow.effective_window_start_ts >= wide.effective_window_start_ts
    assert wide.is_connected is False and narrow.is_connected is False

    # Increasing R widens the request but moves nothing that advancement would
    # consume: E_end and the projected watermark are independent of R.
    for recovery in (3_600, 86_400, 604_800, 2_678_400):
        result = _derive(fire=fire, covered_through=ancient_w, recovery=recovery)
        assert result.effective_window_end_ts == e_end
        assert result.covered_through_ts == ancient_w
        assert _projected_covered_through(result) == e_end


# ---------------------------------------------------------------------------
# 4. Coverage lower bound
# ---------------------------------------------------------------------------

def test_coverage_lower_bound_is_never_moved() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    e_end = fire - timedelta(seconds=D_DEFAULT)
    # A deliberately late lower bound so the derived request starts before it.
    coverage_start = e_end - timedelta(hours=1)
    covered_through = e_end - timedelta(minutes=30)

    result = _derive(
        fire=fire,
        lookback_days=7,
        coverage_start=coverage_start,
        covered_through=covered_through,
    )
    assert result.effective_window_start_ts < result.coverage_start_ts
    # E_start is never clamped to A.
    assert result.coverage_start_ts == coverage_start
    assert result.covered_through_ts == covered_through
    assert result.is_connected is True

    # The result carries exactly the accepted fields — in particular no
    # projected, advanced or re-derived coverage bound, and no bootstrap status
    # or evidence reference.
    assert {f.name for f in dataclasses.fields(EffectiveWindow)} == {
        "scheduled_fire_ts",
        "nominal_window_start_ts",
        "nominal_window_end_ts",
        "effective_window_start_ts",
        "effective_window_end_ts",
        "coverage_start_ts",
        "covered_through_ts",
        "stabilization_delay_seconds",
        "overlap_seconds",
        "max_recovery_span_seconds",
        "is_connected",
    }

    # A > W is refused rather than reordered or ignored.
    _expect_value_error(
        lambda: _derive(
            fire=fire,
            coverage_start=covered_through + SECOND,
            covered_through=covered_through,
        ),
        "coverage_start_ts after covered_through_ts",
    )


# ---------------------------------------------------------------------------
# 5. Configuration boundaries
# ---------------------------------------------------------------------------

def test_configuration_boundaries() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)

    no_delay = _derive(fire=fire, delay=0)
    assert no_delay.effective_window_end_ts == no_delay.nominal_window_end_ts
    assert no_delay.effective_window_start_ts == no_delay.nominal_window_start_ts - (
        timedelta(seconds=O_DEFAULT)
    )

    no_overlap = _derive(fire=fire, overlap=0)
    assert no_overlap.effective_window_start_ts == (
        no_overlap.nominal_window_start_ts - timedelta(seconds=D_DEFAULT)
    )

    minimal_recovery = _derive(fire=fire, overlap=0, recovery=1)
    assert minimal_recovery.effective_window_start_ts == (
        minimal_recovery.effective_window_end_ts - SECOND
    )

    overlap_equals_recovery = _derive(fire=fire, overlap=3_600, recovery=3_600)
    assert overlap_equals_recovery.effective_window_start_ts == (
        overlap_equals_recovery.effective_window_end_ts - timedelta(seconds=3_600)
    )

    # Boundary rejections are covered exhaustively in the input-contract test;
    # the two relationship edges are re-asserted here for locality.
    _expect_value_error(lambda: _derive(fire=fire, overlap=2, recovery=1), "O > R")
    _expect_value_error(lambda: _derive(fire=fire, recovery=0), "R = 0")


# ---------------------------------------------------------------------------
# 6. Closed-interval semantics
# ---------------------------------------------------------------------------

def _duplicated_grid_timestamps(
    first_end: datetime, second_start: datetime
) -> int:
    """Count one-second grid points contained in both closed intervals."""
    if second_start > first_end:
        return 0
    return int((first_end - second_start).total_seconds()) + 1


def test_closed_interval_semantics() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)

    # Shared endpoint: zero duration of overlap, one duplicated grid timestamp.
    shared = _capped_case(fire=fire, grid_steps_after_w=0)
    assert shared.effective_window_start_ts == shared.covered_through_ts
    assert shared.is_connected is True
    assert (
        shared.covered_through_ts - shared.effective_window_start_ts
    ) == timedelta(0)
    assert _duplicated_grid_timestamps(
        shared.covered_through_ts, shared.effective_window_start_ts
    ) == 1

    # One-second adjacency: still connected, zero duplicated timestamps.
    adjacent = _capped_case(fire=fire, grid_steps_after_w=1)
    assert adjacent.is_connected is True
    assert _duplicated_grid_timestamps(
        adjacent.covered_through_ts, adjacent.effective_window_start_ts
    ) == 0

    # Two grid steps: the second W + 1 s belongs to neither interval.
    two_second_gap = _capped_case(fire=fire, grid_steps_after_w=2)
    assert two_second_gap.is_connected is False
    assert _duplicated_grid_timestamps(
        two_second_gap.covered_through_ts,
        two_second_gap.effective_window_start_ts,
    ) == 0

    # `docs/13_…` §2.4: 3600 s of elapsed overlap duration corresponds to 3601
    # duplicated representable second timestamps under closed intervals.
    stale_w = fire - timedelta(days=20)
    hour = _derive(fire=fire, covered_through=stale_w, overlap=3_600)
    assert hour.effective_window_start_ts == stale_w - timedelta(seconds=3_600)
    elapsed = hour.covered_through_ts - hour.effective_window_start_ts
    assert elapsed == timedelta(seconds=3_600)
    assert _duplicated_grid_timestamps(
        hour.covered_through_ts, hour.effective_window_start_ts
    ) == 3_601

    # The +1 s in the contiguity test is a grid step, never an overlap duration:
    # the adjacent case above has a zero-length intersection yet is connected.
    assert (
        adjacent.effective_window_start_ts - adjacent.covered_through_ts
    ) == coverage_windows.CLOSED_INTERVAL_GRID_STEP


# ---------------------------------------------------------------------------
# 7. DST — fires are converted to UTC first, then shifted by absolute seconds
# ---------------------------------------------------------------------------

def _fire_utc(local_date: date, run_time: time, tz: ZoneInfo) -> datetime:
    """Mirror `dispatcher.latest_scheduled_fire_local` fixture construction."""
    return datetime.combine(local_date, run_time, tzinfo=tz).astimezone(UTC)


def _daily_fires(year: int, tz: ZoneInfo, run_time: time) -> list[datetime]:
    fires: list[datetime] = []
    day = date(year, 1, 1)
    while day.year == year:
        fires.append(_fire_utc(day, run_time, tz))
        day += timedelta(days=1)
    return fires


def _weekly_fires(
    year: int, tz: ZoneInfo, run_time: time, weekday: int
) -> list[datetime]:
    return [
        f
        for d, f in (
            (day, _fire_utc(day, run_time, tz))
            for day in _all_days(year)
            if day.weekday() == weekday
        )
    ]


def _all_days(year: int) -> list[date]:
    days: list[date] = []
    day = date(year, 1, 1)
    while day.year == year:
        days.append(day)
        day += timedelta(days=1)
    return days


def _wrong_local_wallclock_start(
    *, fire_local: datetime, lookback_days: int, delay: int, overlap: int
) -> datetime:
    """A deliberately wrong implementation, kept only as a negative control.

    It performs calendar-day subtraction and the `D`/`O` shift in schedule-local
    wall-clock time and converts to UTC last — precisely what `docs/13_…` §11
    and §16.8 forbid. The assertions below must distinguish it from the correct
    result at both Europe/Warsaw transitions.
    """
    naive = fire_local.replace(tzinfo=None) - timedelta(
        days=lookback_days, seconds=delay + overlap
    )
    return naive.replace(tzinfo=fire_local.tzinfo).astimezone(UTC)


def test_dst_warsaw_daily_transitions() -> None:
    run_time = time(2, 0, 0)

    # Spring 2026 (2026-03-29, 02:00 -> 03:00 local): 02:00 does not exist and
    # `fold=0` resolves through the pre-transition offset.
    spring = {
        date(2026, 3, 27): _utc(2026, 3, 27, 1, 0, 0),
        date(2026, 3, 28): _utc(2026, 3, 28, 1, 0, 0),
        date(2026, 3, 29): _utc(2026, 3, 29, 1, 0, 0),
        date(2026, 3, 30): _utc(2026, 3, 30, 0, 0, 0),
    }
    for local_date, expected in spring.items():
        assert _fire_utc(local_date, run_time, WARSAW) == expected, local_date
    assert (
        spring[date(2026, 3, 30)] - spring[date(2026, 3, 29)]
    ) == timedelta(hours=23)

    # Autumn 2026 (2026-10-25, 03:00 -> 02:00 local): 02:00 occurs twice and
    # `fold=0` selects the first (CEST) occurrence.
    autumn = {
        date(2026, 10, 24): _utc(2026, 10, 24, 0, 0, 0),
        date(2026, 10, 25): _utc(2026, 10, 25, 0, 0, 0),
        date(2026, 10, 26): _utc(2026, 10, 26, 1, 0, 0),
    }
    for local_date, expected in autumn.items():
        assert _fire_utc(local_date, run_time, WARSAW) == expected, local_date
    assert (
        autumn[date(2026, 10, 26)] - autumn[date(2026, 10, 25)]
    ) == timedelta(hours=25)

    # DELTA00001 shape (daily, L = 7 d) stays contiguous across both.
    for fires in (sorted(spring.values()), sorted(autumn.values())):
        for previous, following in zip(fires, fires[1:]):
            prior = _derive(fire=previous, lookback_days=7)
            later = _derive(fire=following, lookback_days=7)
            assert (
                later.effective_window_start_ts
                <= prior.effective_window_end_ts + SECOND
            )


def test_dst_utc_schedule_is_unaffected() -> None:
    run_time = time(2, 0, 0)
    utc_tz = ZoneInfo("UTC")
    for around in (date(2026, 3, 28), date(2026, 10, 24)):
        for offset in range(0, 3):
            first = _fire_utc(around + timedelta(days=offset), run_time, utc_tz)
            second = _fire_utc(
                around + timedelta(days=offset + 1), run_time, utc_tz
            )
            assert (second - first) == timedelta(seconds=DAY)
            prior = _derive(fire=first, lookback_days=1)
            later = _derive(fire=second, lookback_days=1)
            assert (
                later.effective_window_start_ts
                <= prior.effective_window_end_ts + SECOND
            )
            # 1 h of slack on every fire, exactly `O`.
            assert (
                prior.effective_window_end_ts - later.effective_window_start_ts
            ) == timedelta(seconds=O_DEFAULT)


def test_bravo00016_weekly_transitions_and_overlap_defect() -> None:
    run_time = time(2, 0, 0)

    spring_prev = _fire_utc(date(2026, 3, 23), run_time, WARSAW)
    spring_next = _fire_utc(date(2026, 3, 30), run_time, WARSAW)
    assert spring_prev == _utc(2026, 3, 23, 1, 0, 0)
    assert spring_next == _utc(2026, 3, 30, 0, 0, 0)
    assert (spring_next - spring_prev) == timedelta(hours=167)

    autumn_prev = _fire_utc(date(2026, 10, 19), run_time, WARSAW)
    autumn_next = _fire_utc(date(2026, 10, 26), run_time, WARSAW)
    assert autumn_prev == _utc(2026, 10, 19, 0, 0, 0)
    assert autumn_next == _utc(2026, 10, 26, 1, 0, 0)
    assert (autumn_next - autumn_prev) == timedelta(seconds=608_400)
    assert (autumn_next - autumn_prev) == timedelta(hours=169)

    # Documented §11.3 instants, exactly. A local-wall-clock implementation
    # cannot produce these.
    spring_prior = _derive(fire=spring_prev, lookback_days=7)
    spring_later = _derive(fire=spring_next, lookback_days=7)
    assert spring_prior.effective_window_end_ts == _utc(2026, 3, 22, 22, 0, 0)
    assert spring_later.effective_window_start_ts == _utc(2026, 3, 22, 20, 0, 0)
    assert (
        spring_prior.effective_window_end_ts
        - spring_later.effective_window_start_ts
    ) == timedelta(hours=2)

    # Autumn with O = 3600: E_start(Oct 26) == E_end(Oct 19) exactly — a shared
    # endpoint, zero overlap duration, one duplicated grid timestamp.
    autumn_prior = _derive(fire=autumn_prev, lookback_days=7, overlap=3_600)
    autumn_later = _derive(fire=autumn_next, lookback_days=7, overlap=3_600)
    assert autumn_prior.effective_window_end_ts == _utc(2026, 10, 18, 21, 0, 0)
    assert (
        autumn_later.effective_window_start_ts
        == autumn_prior.effective_window_end_ts
    )
    assert _duplicated_grid_timestamps(
        autumn_prior.effective_window_end_ts,
        autumn_later.effective_window_start_ts,
    ) == 1

    # O = 3599: adjacent on the grid — still contiguous under the accepted
    # `next_start <= prev_end + 1 s` rule, with zero duplicated timestamps.
    o3599_prior = _derive(fire=autumn_prev, lookback_days=7, overlap=3_599)
    o3599_later = _derive(fire=autumn_next, lookback_days=7, overlap=3_599)
    assert (
        o3599_later.effective_window_start_ts
        == o3599_prior.effective_window_end_ts + SECOND
    )
    assert _duplicated_grid_timestamps(
        o3599_prior.effective_window_end_ts,
        o3599_later.effective_window_start_ts,
    ) == 0

    # O = 3598 is the first value that leaves a real one-second hole.
    o3598_prior = _derive(fire=autumn_prev, lookback_days=7, overlap=3_598)
    o3598_later = _derive(fire=autumn_next, lookback_days=7, overlap=3_598)
    assert (
        o3598_later.effective_window_start_ts
        > o3598_prior.effective_window_end_ts + SECOND
    )

    # Regression guard for the pre-existing autumn defect: with O = 0 the
    # architecture-A arithmetic alone reproduces the documented 3600 s gap.
    zero_prior = _derive(fire=autumn_prev, lookback_days=7, overlap=0)
    zero_later = _derive(fire=autumn_next, lookback_days=7, overlap=0)
    assert (
        zero_later.effective_window_start_ts - zero_prior.effective_window_end_ts
    ) == timedelta(seconds=3_600)


def test_local_wallclock_implementation_is_rejected() -> None:
    run_time = time(2, 0, 0)
    for local_date in (date(2026, 3, 30), date(2026, 10, 26)):
        fire_local = datetime.combine(local_date, run_time, tzinfo=WARSAW)
        correct = _derive(fire=fire_local.astimezone(UTC), lookback_days=7)
        wrong = _wrong_local_wallclock_start(
            fire_local=fire_local,
            lookback_days=7,
            delay=D_DEFAULT,
            overlap=O_DEFAULT,
        )
        assert correct.effective_window_start_ts != wrong, local_date
        assert abs(correct.effective_window_start_ts - wrong) == timedelta(hours=1)


def test_delay_cancels_from_inter_fire_contiguity() -> None:
    run_time = time(2, 0, 0)
    fires = [
        _fire_utc(date(2026, 10, 19), run_time, WARSAW),
        _fire_utc(date(2026, 10, 26), run_time, WARSAW),
    ]
    margins = set()
    for delay in (0, 60, 3_600, 10_800, 86_400, 604_800):
        prior = _derive(fire=fires[0], lookback_days=7, delay=delay)
        later = _derive(fire=fires[1], lookback_days=7, delay=delay)
        margins.add(
            prior.effective_window_end_ts + SECOND - later.effective_window_start_ts
        )
    assert len(margins) == 1, f"D must cancel from contiguity; got {margins!r}"


def test_full_year_2026_sweep_for_production_schedule_shapes() -> None:
    """`E_start(n+1) <= E_end(n) + 1 s` on every 2026 fire, all four shapes."""
    run_time = time(2, 0, 0)
    utc_tz = ZoneInfo("UTC")
    shapes = {
        "DELTA00001": (_daily_fires(2026, WARSAW, run_time), 7),
        "ALPHA00001": (_daily_fires(2026, utc_tz, run_time), 1),
        "FOXTROT00001": (_daily_fires(2026, utc_tz, run_time), 1),
        "BRAVO00016": (_weekly_fires(2026, WARSAW, run_time, 0), 7),
    }
    for client_code, (fires, lookback_days) in shapes.items():
        assert len(fires) >= 52, client_code
        for previous, following in zip(fires, fires[1:]):
            prior = _derive(fire=previous, lookback_days=lookback_days)
            later = _derive(fire=following, lookback_days=lookback_days)
            assert prior.effective_window_start_ts == (
                previous
                - timedelta(seconds=lookback_days * DAY + D_DEFAULT + O_DEFAULT)
            ), (client_code, previous)
            assert (
                later.effective_window_start_ts
                <= prior.effective_window_end_ts + SECOND
            ), (client_code, previous, following)


# ---------------------------------------------------------------------------
# 8. Property and exhaustive boundary checks (deterministic, no new deps)
# ---------------------------------------------------------------------------

def test_monotonicity_and_independence_properties() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    watermarks = [
        fire,
        fire - timedelta(hours=3),
        fire - timedelta(days=1),
        fire - timedelta(days=12),
        fire - timedelta(days=45),
        fire - timedelta(days=400),
    ]
    lookbacks = [0, 1, 2, 7, 31]
    delays = [0, 3_600, 10_800, 86_400]
    overlaps = [0, 1, 3_600, 7_200]
    recoveries = [3_600, 86_400, 604_800, 2_678_400]

    for covered_through in watermarks:
        for lookback in lookbacks:
            for delay in delays:
                # Increasing R can only move E_start earlier or leave it.
                previous_start = None
                for recovery in recoveries:
                    result = _derive(
                        fire=fire,
                        lookback_days=lookback,
                        delay=delay,
                        overlap=0,
                        recovery=recovery,
                        covered_through=covered_through,
                    )
                    if previous_start is not None:
                        assert result.effective_window_start_ts <= previous_start
                    previous_start = result.effective_window_start_ts
                    # E_start never exceeds E_end.
                    assert (
                        result.effective_window_start_ts
                        <= result.effective_window_end_ts
                    )
                    # Projected advancement never moves backward.
                    assert _projected_covered_through(result) >= covered_through
                    # The nominal window depends on F and L only.
                    assert result.nominal_window_end_ts == fire
                    assert result.nominal_window_start_ts == fire - timedelta(
                        seconds=lookback * DAY
                    )

                # Increasing O can only move the pre-cap candidate earlier.
                # R is held at its maximum so the cap never masks the effect.
                previous_start = None
                for overlap in overlaps:
                    result = _derive(
                        fire=fire,
                        lookback_days=lookback,
                        delay=delay,
                        overlap=overlap,
                        recovery=2_678_400,
                        covered_through=covered_through,
                    )
                    if previous_start is not None:
                        assert result.effective_window_start_ts <= previous_start
                    previous_start = result.effective_window_start_ts

    # E_start == E_end is reachable only in the degenerate L = 0, O = 0 case.
    degenerate = _derive(fire=fire, lookback_days=0, overlap=0, delay=0)
    assert (
        degenerate.effective_window_start_ts == degenerate.effective_window_end_ts
    )
    for lookback in (1, 7):
        non_degenerate = _derive(fire=fire, lookback_days=lookback, overlap=0)
        assert (
            non_degenerate.effective_window_start_ts
            < non_degenerate.effective_window_end_ts
        )


def test_delay_shifts_both_bounds_equally() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    far_future_w = fire + timedelta(days=365)
    for lookback in (0, 1, 7):
        reference = _derive(
            fire=fire,
            lookback_days=lookback,
            delay=0,
            covered_through=far_future_w,
        )
        for delay in (1, 3_600, 10_800, 86_400):
            shifted = _derive(
                fire=fire,
                lookback_days=lookback,
                delay=delay,
                covered_through=far_future_w,
            )
            offset = timedelta(seconds=delay)
            assert (
                shifted.effective_window_end_ts
                == reference.effective_window_end_ts - offset
            )
            assert (
                shifted.effective_window_start_ts
                == reference.effective_window_start_ts - offset
            )
            # The nominal window is untouched by D.
            assert (
                shifted.nominal_window_start_ts
                == reference.nominal_window_start_ts
            )
            assert (
                shifted.nominal_window_end_ts == reference.nominal_window_end_ts
            )


def test_nominal_window_independent_of_coverage_and_overlap() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    expected_start = fire - timedelta(seconds=7 * DAY)
    for covered_through in (fire, fire - timedelta(days=200)):
        for overlap, recovery in ((0, 1), (3_600, 3_600), (7_200, 2_678_400)):
            result = _derive(
                fire=fire,
                lookback_days=7,
                overlap=overlap,
                recovery=recovery,
                covered_through=covered_through,
            )
            assert result.nominal_window_start_ts == expected_start
            assert result.nominal_window_end_ts == fire


# ---------------------------------------------------------------------------
# 9. Determinism, immutability and import purity
# ---------------------------------------------------------------------------

def test_determinism_and_immutability() -> None:
    fire = _utc(2026, 5, 12, 2, 0, 0)
    coverage_start = _utc(2026, 4, 1, 0, 0, 0)
    covered_through = _utc(2026, 5, 10, 22, 0, 0)
    inputs = dict(
        scheduled_fire_ts=fire,
        lookback_days=7,
        stabilization_delay_seconds=D_DEFAULT,
        overlap_seconds=O_DEFAULT,
        max_recovery_span_seconds=R_DEFAULT,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
    )
    first = derive_effective_window(**inputs)
    second = derive_effective_window(**inputs)
    assert first == second
    assert first is not second

    # No input object was mutated.
    assert inputs["scheduled_fire_ts"] == fire
    assert inputs["coverage_start_ts"] == coverage_start
    assert inputs["covered_through_ts"] == covered_through
    assert inputs["lookback_days"] == 7

    try:
        first.is_connected = True  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("EffectiveWindow must remain frozen")


def test_module_is_pure_and_scope_bounded() -> None:
    source = (
        REPO_ROOT / "jobs" / "api" / "telematics" / "coverage_windows.py"
    ).read_text(encoding="utf-8")

    # `docs/14_…` C4 requires the module docstring to cite its specification.
    assert "docs/13_telematics_trips_stabilization_windows.md" in source
    assert "§16.1" in source

    # Everything after the `__future__` import is executable code; the module
    # docstring above it legitimately names the C5/C6 surfaces it excludes.
    marker = "from __future__ import annotations"
    assert marker in source
    code = source.split(marker, 1)[1]
    for forbidden in (
        "import os",
        "import psycopg",
        "import requests",
        "import logging",
        "import subprocess",
        "zoneinfo",
        "ZoneInfo",
        "datetime.now",
        "utcnow",
        "os.getenv",
        "os.environ",
        "open(",
        "print(",
        "COVERAGE_WINDOW_EMPTY",
        "SELECT",
        "UPDATE",
        "INSERT",
        "client.log",
        "report_suspected_bug",
    ):
        assert forbidden not in code, forbidden

    # C5 added the gate surfaces to this module; they must stay pure, which the
    # I/O prohibitions above enforce. The vocabulary prohibitions that C4
    # carried (`bootstrap_status`, `TRIPS_COVERAGE_*`, `GAP_DETECTED`) are
    # deliberately retired here rather than deleted wholesale: the module-scope
    # guard in `test_telematics_coverage_state_schema_postgres.py` still bounds
    # exactly which production files may use them.
    for present in (
        "evaluate_coverage_gate",
        "CoverageGateResult",
        "CoverageState",
    ):
        assert hasattr(coverage_windows, present), present

    # C6 keeps mutation surfaces out of the pure helper; only the immutable
    # carrier gains the two approved audit/provenance fields.
    for absent in ("advance_coverage",):
        assert not hasattr(coverage_windows, absent), absent
    assert "advance_coverage" not in code
    assert "covered_through_source" in code
    assert "last_gap_detected_ts" in code


def test_runtime_non_activation() -> None:
    """C5 authorizes exactly one production importer of the helper."""
    completed = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "--untracked",
            "coverage_windows",
            "--",
            "jobs",
            "api",
            "ops",
            "scripts",
            "db",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    referencing = {line for line in completed.stdout.splitlines() if line}
    # The module never names itself in its own body. `dispatcher.py` is the one
    # production importer C5 authorizes; every other production entry here
    # would be an unauthorized integration. The C3 schema suite, the C5 gate
    # suite and the bootstrap-writer suite name it from `ops/tests_manual/`,
    # which is not runtime — the writer suite asserts that a freshly seeded
    # coverage row is exactly what the runtime gate accepts, and the bootstrap
    # tools themselves must never import the helper. The C11 recovery suite
    # names it for one purpose only: proving that the status/mode constants
    # restated in `coverage_finalization.py` have not drifted from the pure
    # helper's definitions. `coverage_finalization.py` itself must stay absent
    # from this set, which is what keeps the helper's importer count at one.
    # The rolling-refetch suite names it from `ops/tests_manual/` for one
    # purpose: proving that `lookback_days` widens the derived window behind
    # the coverage watermark, which is the whole Stream 1 remediation claim.
    # It adds no production importer, so the count stays at one.
    # The M2 DAILY L=3 suite (docs/20 §14 M2) names it for the same reason and
    # from the same non-runtime location: it asserts the approved three-day
    # horizon against the real arithmetic instead of restating the formula.
    assert referencing == {
        "jobs/api/telematics/dispatcher.py",
        "ops/tests_manual/test_telematics_trips_stabilization_windows.py",
        "ops/tests_manual/test_telematics_trips_rolling_refetch.py",
        "ops/tests_manual/test_telematics_daily_lookback_l3.py",
        # The M2 release-contract suite names it to prove that the nominal and
        # effective window starts genuinely differ by D + O — the first-run gate
        # checked the wrong one before independent review caught it (docs/20 §19.10).
        "ops/tests_manual/test_telematics_m2_release_contract.py",
        "ops/tests_manual/test_telematics_coverage_state_schema_postgres.py",
        "ops/tests_manual/test_telematics_coverage_bootstrap_gate.py",
        "ops/tests_manual/test_telematics_coverage_finalization_postgres.py",
        "ops/tests_manual/test_telematics_coverage_bootstrap_writer_postgres.py",
        "ops/tests_manual/test_telematics_trips_recovery_workflow.py",
        # The M4 evidence suite (docs/20 §14 M4) names it for the same reason
        # the M3 suite does: to build a real allowed `CoverageGateResult` as the
        # fixture that lets `_finalize_compat_success` run against a real
        # database. Non-runtime, and it adds no production importer.
        "ops/tests_manual/test_telematics_m4_provider_request_log_postgres.py",
        # The M3 outcome-gate suite (docs/20 §14 M3) names it only to build a
        # real allowed `CoverageGateResult` as the fixture that puts
        # `run_prepared` on the compatibility branch — restating that shape by
        # hand is exactly the drift this module exists to prevent. Non-runtime
        # location, no production importer added, count still one.
        "ops/tests_manual/test_telematics_dispatcher_execution_outcome_gate.py",
        # The M5 multi-cadence suite (docs/20 §14 M5) names it to prove that
        # M5's re-key changed which coverage row the arithmetic addresses and
        # NOT the arithmetic itself — `E_start`, `E_end` and `W` are asserted
        # unchanged against the real helper. Non-runtime, no production
        # importer added. THIS ENTRY WAS MISSING: M5 added the reference
        # without extending this set, so `test_runtime_non_activation` has been
        # failing since `b682df9`. Adding it here is the correction, not a
        # relaxation — the invariant it guards (exactly one production
        # importer) is unchanged and still holds.
        "ops/tests_manual/test_telematics_m5_multi_cadence_identity_postgres.py",
        # The M6 weekly-reconciliation suite (docs/20 §14 M6) names it for two
        # non-runtime reasons: to assert that `L = 16` derives the intended
        # window and is not clamped by the current `R`, and to assert that the
        # `READY` constant restated in `schedule_mutation_surfaces.py` has not
        # drifted from the pure helper. That restatement is deliberate — an
        # import there would have made the policy oracle a SECOND production
        # importer and broken exactly the invariant this test defends.
        "ops/tests_manual/test_telematics_m6_weekly_reconciliation.py",
    }, referencing
    assert (
        REPO_ROOT / "jobs" / "api" / "telematics" / "coverage_windows.py"
    ).is_file()


def main() -> None:
    test_nominal_arithmetic()
    test_input_contract_rejections()
    test_normal_connected_operation()
    test_recovery_span_cap()
    test_coverage_lower_bound_is_never_moved()
    test_configuration_boundaries()
    test_closed_interval_semantics()
    test_dst_warsaw_daily_transitions()
    test_dst_utc_schedule_is_unaffected()
    test_bravo00016_weekly_transitions_and_overlap_defect()
    test_local_wallclock_implementation_is_rejected()
    test_delay_cancels_from_inter_fire_contiguity()
    test_full_year_2026_sweep_for_production_schedule_shapes()
    test_monotonicity_and_independence_properties()
    test_delay_shifts_both_bounds_equally()
    test_nominal_window_independent_of_coverage_and_overlap()
    test_determinism_and_immutability()
    test_module_is_pure_and_scope_bounded()
    test_runtime_non_activation()
    print("OK - Telematics stabilized schedule-window derivation checks passed")


if __name__ == "__main__":
    main()
