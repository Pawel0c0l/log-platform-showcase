#!/usr/bin/env python3
"""M-LAG — the durable observed-delivery-lag trace.

Specification of record: ``docs/21_telematics_delivery_lag_trace.md``.

What this suite owns, stated so it is not mistaken for a coverage or ingestion
suite: the durability and immutability of the first-observation instant, the
exact lag semantics including the negative and NULL cases, and the correctness of
the recomputed daily slice under a MOVING ``end_timestamp`` — which is the one
genuinely hard property of this milestone and the reason the aggregate is a
recompute rather than a counter.

Run::

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_telematics_mlag_delivery_lag.py

PostgreSQL checks run only when ``TELEMATICS_MLAG_TEST_DSN`` names a disposable
loopback database; everything else is pure and always runs.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics.delivery_lag import (  # noqa: E402
    BUCKET_NAMES,
    DEFAULT_OVERLAP_SECONDS,
    DEFAULT_STABILIZATION_DELAY_SECONDS,
    DISCOVERY_COLUMNS,
    DISCOVERY_UNATTRIBUTED,
    OBSERVED_AFTER_TRIP_END,
    OBSERVED_BEFORE_TRIP_END,
    PROVENANCE_ABSENT,
    SECONDS_PER_DAY,
    TripObservation,
    bucket_for,
    build_daily_slice,
    classify_lag,
    discovery_column_for,
    guaranteed_horizon_seconds,
    m6_weekly_guarantee_seconds,
    observed_delivery_lag_seconds,
    percentile_nearest_rank,
    recompute_horizon_days,
    weekly_guarantee_from_schedules,
)

ENV = "TELEMATICS_MLAG_TEST_DSN"
MIGRATIONS = ROOT / "db" / "migrations"
CLIENT_MIGRATIONS = ROOT / "db" / "client_business"
PLATFORM_MIGRATION = "063_workflow_a_trip_delivery_lag_daily.sql"
CLIENT_MIGRATION = "048_client_trips_first_seen_response_received_at.sql"

#: The EXPAND-window constraint: an instant may not exist without an identity.
EXPAND_CONSTRAINT = "ck_client_trips_first_seen_instant_needs_request"
#: The later CONTRACT invariant, installed only by the closure tool.
STRICT_CONSTRAINT = "ck_client_trips_first_seen_pairing"

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc

_failures: List[str] = []


def _check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        if detail:
            print(f"      {detail}")
        _failures.append(label)


def _raises(label: str, fn, exc=ValueError) -> None:
    try:
        fn()
    except exc:
        _check(label, True)
    except Exception as other:  # noqa: BLE001
        _check(label, False, f"raised {type(other).__name__}: {other}")
    else:
        _check(label, False, "it was accepted")


# ===========================================================================
# The metric
# ===========================================================================

def test_the_lag_formula() -> None:
    end = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    _check(
        "ordinary positive lag is observation minus trip end",
        observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=end + timedelta(hours=13, minutes=47),
            end_timestamp=end,
        ) == 13 * 3600 + 47 * 60,
    )
    _check(
        "a multi-day lag is measured in whole seconds, not days",
        observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=end + timedelta(days=8, hours=14),
            end_timestamp=end,
        ) == 8 * SECONDS_PER_DAY + 14 * 3600,
    )
    _check(
        "a trip observed before its end yields a NEGATIVE lag, never zero",
        observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=end - timedelta(hours=2),
            end_timestamp=end,
        ) == -7200,
    )
    _check(
        "no provenance yields None, not zero and not an exception",
        observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=None, end_timestamp=end,
        ) is None,
    )
    _check(
        "no trip end yields None as well",
        observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=end, end_timestamp=None,
        ) is None,
    )
    # Timezone safety: the same two instants expressed in different zones must
    # give the same answer, because a lag is a property of instants.
    utc_pair = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
        end_timestamp=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )
    warsaw_pair = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=(
            datetime(2026, 7, 22, 8, 0, tzinfo=UTC).astimezone(WARSAW)
        ),
        end_timestamp=datetime(2026, 7, 21, 10, 0, tzinfo=UTC).astimezone(WARSAW),
    )
    _check(
        "the lag is timezone-invariant — same instants, same answer",
        utc_pair == warsaw_pair == 22 * 3600,
        f"{utc_pair} vs {warsaw_pair}",
    )
    _raises(
        "a naive observation instant is refused, never assumed to be UTC",
        lambda: observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=datetime(2026, 7, 22, 8, 0),
            end_timestamp=end,
        ),
    )
    _raises(
        "a naive trip end is refused too",
        lambda: observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=end,
            end_timestamp=datetime(2026, 7, 21, 10, 0),
        ),
    )


def test_the_lag_changes_when_end_timestamp_is_corrected() -> None:
    """The reason the lag is derived and never stored."""
    observed = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    open_end = datetime(2026, 7, 21, 11, 0, tzinfo=UTC)
    corrected_end = datetime(2026, 7, 21, 15, 0, tzinfo=UTC)

    before = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=observed, end_timestamp=open_end,
    )
    after = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=observed, end_timestamp=corrected_end,
    )
    _check(
        "correcting end_timestamp changes the lag, with first-seen fixed",
        before == 3600 and after == -3 * 3600,
        f"before={before} after={after}",
    )
    _check(
        "and the correction can flip an ordinary lag into the negative class",
        classify_lag(before) == OBSERVED_AFTER_TRIP_END
        and classify_lag(after) == OBSERVED_BEFORE_TRIP_END,
    )
    _check(
        "a None lag classifies as absent provenance, never as zero lag",
        classify_lag(None) == PROVENANCE_ABSENT,
    )


# ===========================================================================
# The guarantee boundary
# ===========================================================================

def test_the_guarantee_boundary_is_derived_not_hard_coded() -> None:
    _check(
        "the M6 weekly guarantee is 792 000 s = 9.1667 d",
        m6_weekly_guarantee_seconds() == 792_000,
        f"got {m6_weekly_guarantee_seconds()}",
    )
    _check(
        "and it equals L + (D+O)/86400 - P computed from the tuning",
        m6_weekly_guarantee_seconds() == guaranteed_horizon_seconds(
            lookback_days=16, cadence_period_days=7,
            stabilization_delay_seconds=DEFAULT_STABILIZATION_DELAY_SECONDS,
            overlap_seconds=DEFAULT_OVERLAP_SECONDS,
        ),
    )
    _check(
        "the constant is (D+O)/86400 = 0.1667 d, not the withdrawn 0.208",
        abs(
            (m6_weekly_guarantee_seconds() / SECONDS_PER_DAY) - (16 - 7 + 1 / 6)
        ) < 1e-9,
        f"{m6_weekly_guarantee_seconds() / SECONDS_PER_DAY}",
    )
    _raises(
        "a cadence that guarantees nothing cannot yield a boundary",
        lambda: guaranteed_horizon_seconds(
            lookback_days=7, cadence_period_days=7,
            stabilization_delay_seconds=0, overlap_seconds=0,
        ),
    )
    _check(
        "the best live reconciliation cadence wins",
        weekly_guarantee_from_schedules(
            reconciliation_cadences=[(16, 7), (46, 31)]
        ) == guaranteed_horizon_seconds(
            lookback_days=46, cadence_period_days=31,
            stabilization_delay_seconds=DEFAULT_STABILIZATION_DELAY_SECONDS,
            overlap_seconds=DEFAULT_OVERLAP_SECONDS,
        ),
    )
    _check(
        "with no reconciliation cadence enabled it falls back to M6's boundary",
        weekly_guarantee_from_schedules(reconciliation_cadences=[])
        == m6_weekly_guarantee_seconds(),
        "the bucket edges must be stable from the first slice, not shift the "
        "day a schedule is enabled",
    )
    _check(
        "a cadence guaranteeing nothing is ignored rather than fatal",
        weekly_guarantee_from_schedules(reconciliation_cadences=[(7, 7), (16, 7)])
        == m6_weekly_guarantee_seconds(),
    )


def test_the_recompute_horizon_follows_the_deepest_enabled_lookback() -> None:
    _check(
        "today's DAILY-only fleet: deepest lookback 7 -> 9 days",
        recompute_horizon_days(enabled_lookback_days=[3, 7, 1]) == 9,
    )
    _check(
        "with M6 enabled: deepest lookback 16 -> 18 days",
        recompute_horizon_days(enabled_lookback_days=[3, 16]) == 18,
    )
    _check(
        "no schedules at all still recomputes the margin",
        recompute_horizon_days(enabled_lookback_days=[]) == 2,
    )
    _check(
        "the horizon is never zero — the newest slice must always be rewritten",
        recompute_horizon_days(enabled_lookback_days=[], margin_days=0) == 1,
    )
    _raises(
        "a non-integer lookback is refused",
        lambda: recompute_horizon_days(enabled_lookback_days=[3.5]),
    )


# ===========================================================================
# Buckets and percentiles
# ===========================================================================

def test_bucket_boundaries() -> None:
    g = m6_weekly_guarantee_seconds()
    cases = [
        (-1, "bucket_negative"),
        (0, "bucket_under_6h"),
        (6 * 3600 - 1, "bucket_under_6h"),
        (6 * 3600, "bucket_6h_to_24h"),
        (24 * 3600 - 1, "bucket_6h_to_24h"),
        (24 * 3600, "bucket_1d_to_3d"),
        (3 * SECONDS_PER_DAY - 1, "bucket_1d_to_3d"),
        (3 * SECONDS_PER_DAY, "bucket_3d_to_7d"),
        (7 * SECONDS_PER_DAY - 1, "bucket_3d_to_7d"),
        (7 * SECONDS_PER_DAY, "bucket_7d_to_weekly_guarantee"),
        (g - 1, "bucket_7d_to_weekly_guarantee"),
        (g, "bucket_weekly_guarantee_to_15d"),
        (15 * SECONDS_PER_DAY - 1, "bucket_weekly_guarantee_to_15d"),
        (15 * SECONDS_PER_DAY, "bucket_over_15d"),
    ]
    wrong = [
        (lag, expected, bucket_for(lag, weekly_guarantee_seconds=g))
        for lag, expected in cases
        if bucket_for(lag, weekly_guarantee_seconds=g) != expected
    ]
    _check(
        "every boundary lands in the upper bucket (half-open upward)",
        not wrong,
        f"{wrong}",
    )
    _check(
        "the directly proven July maximum (8.608 d) is INSIDE the M6 guarantee",
        bucket_for(int(8.608 * SECONDS_PER_DAY), weekly_guarantee_seconds=g)
        == "bucket_7d_to_weekly_guarantee",
        "if this moves, the guarantee no longer covers the retained evidence",
    )
    _check(
        "the bucket vocabulary matches the migration's column list",
        set(BUCKET_NAMES) == {
            "bucket_negative", "bucket_under_6h", "bucket_6h_to_24h",
            "bucket_1d_to_3d", "bucket_3d_to_7d",
            "bucket_7d_to_weekly_guarantee",
            "bucket_weekly_guarantee_to_15d", "bucket_over_15d",
        },
    )


def test_percentiles_are_observed_values() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    _check("p50 of 10 values is the 5th", percentile_nearest_rank(values, p=50) == 50)
    _check("p90 is the 9th", percentile_nearest_rank(values, p=90) == 90)
    _check("p95 is the 10th", percentile_nearest_rank(values, p=95) == 100)
    _check("p100 is the max", percentile_nearest_rank(values, p=100) == 100)
    _check("a single-value sample returns that value",
           percentile_nearest_rank([7], p=50) == 7)
    _check(
        "input order does not matter",
        percentile_nearest_rank(list(reversed(values)), p=90) == 90,
    )
    _check(
        "every percentile is a value that actually occurred",
        all(
            percentile_nearest_rank(values, p=p) in values
            for p in (1, 25, 50, 90, 95, 99, 100)
        ),
    )
    _raises("an empty sample has no percentile",
            lambda: percentile_nearest_rank([], p=50))


# ===========================================================================
# The slice
# ===========================================================================

def _obs(tid: int, *, end: datetime, seen: datetime = None, role: str = None):
    return TripObservation(
        provider_trip_id=tid,
        end_timestamp=end,
        first_seen_response_received_at_utc=seen,
        first_seen_run_type=role,
    )


def test_the_slice_partitions_everything() -> None:
    g = m6_weekly_guarantee_seconds()
    end = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    observations = [
        _obs(1, end=end, seen=end + timedelta(hours=1), role="DAILY"),
        _obs(2, end=end, seen=end + timedelta(hours=10), role="DAILY"),
        _obs(3, end=end, seen=end + timedelta(days=2), role="DAILY"),
        _obs(4, end=end, seen=end + timedelta(days=8), role="WEEKLY_RECONCILIATION"),
        _obs(5, end=end, seen=end + timedelta(days=20), role="MONTHLY_RECONCILIATION"),
        _obs(6, end=end, seen=end - timedelta(hours=3), role="DAILY"),
        # provenance never captured — pre-M4 / recovery shape
        _obs(7, end=end),
        # provenance present, evidence no longer resolvable
        _obs(8, end=end, seen=end + timedelta(days=1), role=None),
    ]
    s = build_daily_slice(
        trip_end_date=date(2026, 7, 21),
        observations=observations,
        weekly_guarantee_seconds=g,
    )
    _check("trips_total counts every trip in the slice", s.trips_total == 8)
    _check(
        "trips_with_provenance excludes only the row with no first-seen instant",
        s.trips_with_provenance == 7,
    )
    _check(
        "buckets sum to trips_with_provenance (migration 063's CHECK)",
        sum(s.buckets.values()) == s.trips_with_provenance,
        f"{s.buckets}",
    )
    _check(
        "attribution sums to trips_with_provenance too",
        sum(s.discovery.values()) == s.trips_with_provenance,
        f"{s.discovery}",
    )
    _check("the negative observation is its own bucket",
           s.buckets["bucket_negative"] == 1)
    _check("the 8-day arrival lands under the weekly guarantee",
           s.buckets["bucket_7d_to_weekly_guarantee"] == 1)
    _check("the 20-day arrival lands over 15 d",
           s.buckets["bucket_over_15d"] == 1)
    _check(
        "the weekly cadence is credited with exactly its own discovery",
        s.discovery["discovered_weekly_reconciliation"] == 1
        and s.discovery["discovered_monthly_reconciliation"] == 1,
    )
    _check(
        "unresolvable evidence is unattributed, NOT silently credited to DAILY",
        s.discovery[DISCOVERY_UNATTRIBUTED] == 1
        and s.discovery["discovered_daily"] == 4,
        f"{s.discovery}",
    )
    _check(
        "min is the negative value and max is the 20-day one",
        s.lag_min_seconds == -3 * 3600
        and s.lag_max_seconds == 20 * SECONDS_PER_DAY,
    )
    _check(
        "the stored guarantee is the boundary the buckets were computed against",
        s.weekly_guarantee_seconds == g,
    )
    _check(
        "as_row() emits exactly the migration's metric column names",
        set(s.as_row()) == (
            {"trip_end_date", "trips_total", "trips_with_provenance",
             "trips_provenance_pending",
             "lag_p50_seconds", "lag_p90_seconds", "lag_p95_seconds",
             "lag_max_seconds", "lag_min_seconds", "weekly_guarantee_seconds"}
            | set(BUCKET_NAMES)
            | set(DISCOVERY_COLUMNS.values()) | {DISCOVERY_UNATTRIBUTED}
        ),
    )


def test_an_empty_slice_has_no_percentiles() -> None:
    s = build_daily_slice(
        trip_end_date=date(2026, 7, 21), observations=[],
        weekly_guarantee_seconds=m6_weekly_guarantee_seconds(),
    )
    _check(
        "an empty slice reports no percentile rather than a fabricated zero",
        s.trips_total == 0 and s.trips_with_provenance == 0
        and s.lag_p50_seconds is None and s.lag_max_seconds is None,
    )
    end = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    s2 = build_daily_slice(
        trip_end_date=date(2026, 7, 21), observations=[_obs(1, end=end)],
        weekly_guarantee_seconds=m6_weekly_guarantee_seconds(),
    )
    _check(
        "a slice of only unprovenanced trips likewise has no percentiles",
        s2.trips_total == 1 and s2.trips_with_provenance == 0
        and s2.lag_p50_seconds is None,
    )


def test_the_slice_is_deterministic_and_dedupes() -> None:
    g = m6_weekly_guarantee_seconds()
    end = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    obs = [
        _obs(i, end=end, seen=end + timedelta(hours=i), role="DAILY")
        for i in range(1, 21)
    ]
    a = build_daily_slice(trip_end_date=date(2026, 7, 21), observations=obs,
                          weekly_guarantee_seconds=g)
    b = build_daily_slice(trip_end_date=date(2026, 7, 21),
                          observations=list(reversed(obs)),
                          weekly_guarantee_seconds=g)
    _check(
        "recomputing a slice is deterministic — the upsert is idempotent",
        a.as_row() == b.as_row(),
    )
    dup = build_daily_slice(
        trip_end_date=date(2026, 7, 21), observations=obs + obs,
        weekly_guarantee_seconds=g,
    )
    _check(
        "a duplicated trip cannot double-count",
        dup.as_row() == a.as_row(),
        f"{dup.trips_total} vs {a.trips_total}",
    )
    _raises(
        "a non-positive guarantee boundary is refused",
        lambda: build_daily_slice(
            trip_end_date=date(2026, 7, 21), observations=obs,
            weekly_guarantee_seconds=0,
        ),
    )


def test_a_moved_trip_is_self_correcting_across_two_slices() -> None:
    """The design gate, proven at the aggregation level.

    A trip observed while still open, whose `end_timestamp` is later corrected
    across a date boundary, must leave the old slice and enter the new one. With
    an append-only counter it would sit in both.
    """
    g = m6_weekly_guarantee_seconds()
    observed = datetime(2026, 7, 21, 23, 30, tzinfo=UTC)
    open_end = datetime(2026, 7, 21, 23, 0, tzinfo=UTC)
    corrected_end = datetime(2026, 7, 22, 0, 30, tzinfo=UTC)

    def slices(end_ts, day):
        return build_daily_slice(
            trip_end_date=day,
            observations=[_obs(1, end=end_ts, seen=observed, role="DAILY")]
            if end_ts.astimezone(UTC).date() == day else [],
            weekly_guarantee_seconds=g,
        )

    d21, d22 = date(2026, 7, 21), date(2026, 7, 22)
    before_21 = slices(open_end, d21)
    before_22 = slices(open_end, d22)
    _check(
        "before correction the trip is in the 21st only",
        before_21.trips_total == 1 and before_22.trips_total == 0,
    )
    after_21 = slices(corrected_end, d21)
    after_22 = slices(corrected_end, d22)
    _check(
        "after correction it is in the 22nd only — no residue in the 21st",
        after_21.trips_total == 0 and after_22.trips_total == 1,
        f"21st={after_21.trips_total} 22nd={after_22.trips_total}",
    )
    _check(
        "and its lag moved with the end timestamp, going negative",
        after_22.lag_max_seconds == -3600
        and before_21.lag_max_seconds == 1800,
        f"{before_21.lag_max_seconds} -> {after_22.lag_max_seconds}",
    )
    _check(
        "the immutable observation instant did not move",
        observed == datetime(2026, 7, 21, 23, 30, tzinfo=UTC),
    )


def test_attribution_never_invents_a_role() -> None:
    _check("a known role maps to its own column",
           discovery_column_for("WEEKLY_RECONCILIATION")
           == "discovered_weekly_reconciliation")
    for missing in (None, "", "   ", "UNKNOWN_ROLE", "daily"):
        _check(
            f"{missing!r} is unattributed, not DAILY",
            discovery_column_for(missing) == DISCOVERY_UNATTRIBUTED,
        )


# ===========================================================================
# Static contracts against the real ingestion path
# ===========================================================================

def _sql_literals(path: Path, needle: str) -> List[str]:
    source = path.read_text(encoding="utf-8")
    return [
        node.value
        for node in ast.walk(ast.parse(source, path.name))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and needle in node.value
    ]


def test_the_trip_upsert_writes_both_halves_and_updates_neither() -> None:
    path = ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py"
    literals = _sql_literals(path, "ON CONFLICT")
    _check("there is an upsert literal to inspect", bool(literals))

    # The trip upsert is an f-string whose table name AND several column groups
    # are interpolated, so `INSERT INTO`, the leading columns and the trailing
    # columns are all separate AST constants. Matched on the trailing column
    # group, which is the one both first-seen columns live in.
    _check(
        "the trip INSERT statement exists",
        bool(_sql_literals(path, "INSERT INTO ")),
    )
    tail = _sql_literals(path, "sync_run_id,")
    _check(
        "the trip INSERT's trailing column group was located",
        bool(tail),
        "the assertions below would pass vacuously otherwise",
    )
    _check(
        "the INSERT column list names first_seen_response_received_at_utc",
        any("first_seen_response_received_at_utc" in t for t in tail),
        "the durable instant must be written by the normal ingestion path",
    )
    _check(
        "and it names first_seen_request_id alongside it",
        any(
            "first_seen_request_id" in t
            and "first_seen_response_received_at_utc" in t
            for t in tail
        ),
        "both halves must be in the SAME column list, so no ingestion path can "
        "write one without the other",
    )
    update_clauses = [
        t.split("DO UPDATE SET", 1)[1] for t in literals if "DO UPDATE SET" in t
    ]
    _check("there is a DO UPDATE SET clause to inspect", bool(update_clauses))
    offenders = [
        c.strip()[:120] for c in update_clauses
        if "first_seen_response_received_at_utc" in c
        or "first_seen_request_id" in c
    ]
    _check(
        "NEITHER first-seen column appears in any DO UPDATE SET",
        not offenders,
        f"a rediscovery would restate the first observation: {offenders}",
    )
    _check(
        "end_timestamp IS updated — which is why the lag must stay derived",
        any("end_timestamp=EXCLUDED.end_timestamp" in c for c in update_clauses),
    )


def test_the_two_halves_come_from_one_mapping_entry() -> None:
    """They cannot be populated independently, by construction."""
    from jobs.api.telematics.request_evidence import RequestEvidenceCollector

    start = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)
    c = RequestEvidenceCollector(
        effective_window_start_ts=start,
        effective_window_end_ts=start + timedelta(days=1),
    )
    c.begin_tiling_unit(
        index=1, covers_from=start, covers_to=start + timedelta(days=1),
        requested_from=start, requested_to=start + timedelta(days=1),
    )
    c.begin_subwindow(
        label="w1", requested_from=start,
        requested_to=start + timedelta(days=1),
        wire_start="2026-07-21 04:00:00", wire_end="2026-07-22 03:59:59",
    )
    received = start + timedelta(minutes=1)
    rid = c.record_page(
        page=1, request_started_at_utc=start,
        response_received_at_utc=received, http_status=200, row_count=2,
    )
    c.record_first_seen(request_id=rid, identities=[111, 222])

    ids = c.first_seen_request_ids()
    obs = c.first_seen_observations()
    _check(
        "both accessors describe the same trips",
        set(ids) == set(obs) == {111, 222},
    )
    _check(
        "the instant is the page's response_received_at_utc, not its start",
        all(pair[1] == received for pair in obs.values())
        and received != start,
    )
    _check(
        "the identity is the same in both accessors",
        all(obs[t][0] == ids[t] for t in ids),
    )

    # A later page must not restate the first sighting.
    later = received + timedelta(minutes=5)
    rid2 = c.record_page(
        page=2, request_started_at_utc=later,
        response_received_at_utc=later + timedelta(seconds=10),
        http_status=200, row_count=1,
    )
    c.record_first_seen(request_id=rid2, identities=[111, 333])
    obs2 = c.first_seen_observations()
    _check(
        "a re-observed trip keeps its first sighting",
        obs2[111] == obs[111],
    )
    _check(
        "a newly seen trip gets the later page's instant",
        obs2[333][0] == rid2 and obs2[333][1] == later + timedelta(seconds=10),
    )
    _check(
        "an unknown request_id binds nothing rather than binding a wrong instant",
        (
            c.record_first_seen(
                request_id="db8055e0-e030-4d5a-816b-ec4dc338d698",
                identities=[444],
            ) is None
        ) and 444 not in c.first_seen_observations(),
    )


def test_only_the_insert_only_backfill_is_a_no_provenance_path() -> None:
    """Corrected: C11 is NOT a no-provenance path.

    An earlier revision of this suite, and of docs/21, treated
    `ops/recover_telematics_trips_window.py` as permanently NULL-provenance because
    the tool itself contains no first-seen SQL. That reasoning was wrong. C11 does
    not write trips at all — it LAUNCHES `jobs.api.telematics.sync_trips_and_speeding`
    as a subprocess (`SYNC_JOB_MODULE`), which is the normal writer. A C11 recovery
    therefore captures normal first-seen provenance whenever request evidence
    exists, and its rows are ordinary provenance-bearing rows.

    The genuine no-provenance path is the insert-only backfill, which writes trips
    directly with no provider request evidence to point at.
    """
    backfill = (
        ROOT / "jobs" / "api" / "telematics" / "backfill_trips_insert_only.py"
    ).read_text(encoding="utf-8")
    _check(
        "the insert-only backfill writes neither first-seen column",
        "first_seen_request_id" not in backfill
        and "first_seen_response_received_at_utc" not in backfill,
        "it has no provider request evidence to point at, so both halves must "
        "stay NULL — never imputed",
    )

    recovery = (ROOT / "ops" / "recover_telematics_trips_window.py").read_text(
        encoding="utf-8"
    )
    _check(
        "C11 launches the normal writer rather than writing trips itself",
        "SYNC_JOB_MODULE = \"jobs.api.telematics.sync_trips_and_speeding\"" in recovery,
        "if C11 stopped delegating to the sync job, its provenance semantics "
        "would change and docs/21 §4 would need revisiting",
    )
    _check(
        "C11 issues no client_trips INSERT of its own",
        "INSERT INTO public.client_trips" not in recovery
        and "INSERT INTO client_trips" not in recovery,
        "the absence of first-seen SQL here is because it writes no trips, NOT "
        "because its trips lack provenance",
    )


def test_the_migration_declares_its_release_requirement() -> None:
    """A release naming the column must refuse to activate without it."""
    data = json.loads((ROOT / "db" / "schema_requirements.json").read_text())
    entries = {r["migration"]: r for r in data["requirements"]}
    _check(
        "client migration 048 is a declared release prerequisite",
        CLIENT_MIGRATION in entries,
        "the trip INSERT names the column unconditionally — the same failure "
        "mode 047 introduced",
    )
    if CLIENT_MIGRATION in entries:
        rel = entries[CLIENT_MIGRATION]["relations"][0]
        cols = {c["name"] for c in rel["columns"]}
        unconditional = [
            c["name"] if isinstance(c, dict) else c
            for c in rel.get("constraints", [])
        ]
        alternatives = {
            group["state"]: {c["name"]: c for c in group["constraints"]}
            for group in rel.get("constraint_alternatives", [])
        }
        _check(
            "it declares the column",
            "first_seen_response_received_at_utc" in cols,
        )
        # The BRIDGE model. This release is activatable on either side of the
        # CONTRACT closure, so the pairing constraint is declared as two exactly
        # specified alternative STATES rather than as one required constraint.
        # Declaring only EXPAND would make the release un-activatable the moment
        # the closure drops it; declaring only the strict constraint would make
        # it un-activatable until the closure creates it.
        _check(
            "it declares the EXPAND state, not as an unconditional requirement",
            EXPAND_CONSTRAINT in alternatives.get("EXPAND", {})
            and EXPAND_CONSTRAINT not in unconditional,
        )
        _check(
            "the EXPAND state expects the constraint NOT VALID, as 048 leaves it",
            alternatives.get("EXPAND", {}).get(
                EXPAND_CONSTRAINT, {}
            ).get("validated") is False,
        )
        _check(
            "it declares the strict pairing constraint ONLY as the CONTRACT state",
            STRICT_CONSTRAINT in alternatives.get("CONTRACT", {})
            and STRICT_CONSTRAINT not in unconditional
            and STRICT_CONSTRAINT not in alternatives.get("EXPAND", {}),
            "requiring strict pairing unconditionally at release activation "
            "would demand a state the rollout has not reached",
        )
        _check(
            "and the CONTRACT state is accepted only when VALIDATED",
            alternatives.get("CONTRACT", {}).get(
                STRICT_CONSTRAINT, {}
            ).get("validated") is True,
            "a strict constraint that exists but is still NOT VALID is an "
            "interrupted closure, not a closed contract",
        )
        _check(
            "and it is client_business scope",
            entries[CLIENT_MIGRATION]["scope"] == "client_business",
        )
    _check(
        "the release declares the post-CONTRACT writer capability",
        "client_trips_first_seen_pair_contract" in data.get("capabilities", []),
        "after the closure the strict constraint rejects a request-only INSERT, "
        "so only a release declaring this capability may activate at all",
    )
    _check(
        "platform migration 063 is declared too",
        PLATFORM_MIGRATION in entries
        and entries.get(PLATFORM_MIGRATION, {}).get("scope") == "platform",
    )


def test_the_aggregation_tool_makes_no_provider_call() -> None:
    text = (ROOT / "ops" / "aggregate_telematics_delivery_lag.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "TelematicsFleetProviderClient", "requests.", "urllib", "http.client",
        "subprocess",
    ):
        _check(
            f"the aggregation tool does not reference {forbidden}",
            forbidden not in text,
        )
    _check(
        "it writes only the slice relation",
        "UPDATE public.client_trips" not in text
        and "INSERT INTO public.client_trips" not in text
        and "client_dataset_coverage" not in text
        and "INSERT INTO workflow_a_control.trip_delivery_lag_daily" in text,
    )
    _check(
        "it is dry-run unless both --execute and --confirm RECOMPUTE are given",
        'str(args.confirm or "") != "RECOMPUTE"' in text,
    )


def test_no_second_lag_calculation_exists() -> None:
    """One formula, in one module."""
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "snapshots/", "tmp/", "backups/", "release")):
            continue
        if rel in (
            "jobs/api/telematics/delivery_lag.py",
            "ops/tests_manual/test_telematics_mlag_delivery_lag.py",
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "first_seen_response_received_at_utc" not in text:
            continue
        if "- end_timestamp" in text or "-end_timestamp" in text:
            offenders.append(rel)
    _check(
        "the subtraction appears in exactly one module",
        not offenders,
        f"a second calculation would drift on the NULL or negative case: "
        f"{offenders}",
    )


# ===========================================================================
# PostgreSQL
# ===========================================================================

def _apply(conn, path: Path) -> None:
    conn.execute(path.read_text(encoding="utf-8"))


def _drop_client_tables(conn) -> None:
    """Migrations 020/021 leave `client_trips_rebuilt_*` behind.

    Dropping only `client_trips` leaves those, and their primary keys then
    collide when the chain is replayed — which makes the suite pass once and
    fail on a second run against the same database. Dropping every
    `client_trips%` relation makes it genuinely re-runnable.
    """
    rows = conn.execute(
        """SELECT tablename FROM pg_tables
            WHERE schemaname = 'public' AND tablename LIKE 'client_trips%'"""
    ).fetchall()
    for (name,) in rows:
        conn.execute(f'DROP TABLE IF EXISTS public."{name}" CASCADE')
    conn.commit()


CLIENT_PREREQ = (
    "009_workflow_a_client_business.sql", "010_add_client_code.sql",
    "011_extend_client_trips.sql", "014_add_record_id_and_synced_at.sql",
    "015_add_rpm_columns.sql", "016_add_odometer_columns.sql",
    "018_client_trips_no_fuel.sql", "019_add_speeding_violation_count_columns.sql",
    "020_client_trips_final_schema.sql", "021_add_trip_mode_to_client_trips.sql",
    "026_add_driver_restrictions_to_client_trips.sql",
    "047_client_trips_first_seen_request_id.sql",
)


def _client_schema(conn) -> None:
    _drop_client_tables(conn)
    for name in CLIENT_PREREQ:
        p = CLIENT_MIGRATIONS / name
        if p.exists():
            try:
                _apply(conn, p)
            except Exception:
                conn.rollback()
    _apply(conn, CLIENT_MIGRATIONS / CLIENT_MIGRATION)
    conn.commit()


def test_client_migration_is_additive_and_null_compatible(conn) -> None:
    import psycopg

    # A pre-existing row written BEFORE the column existed must survive.
    _drop_client_tables(conn)
    for name in CLIENT_PREREQ:
        p = CLIENT_MIGRATIONS / name
        if p.exists():
            try:
                _apply(conn, p)
            except Exception:
                conn.rollback()
    # THE LIVE SHAPE THIS MIGRATION MUST TOLERATE. 047 already shipped and the
    # deployed M4 writer already populates `first_seen_request_id`, so a real
    # pre-048 table contains rows with an identity and NO instant column at all.
    # A migration that assumed both halves were NULL could not be applied.
    conn.execute(
        """INSERT INTO public.client_trips
           (client_id, provider_trip_id, registration, end_timestamp,
            first_seen_request_id)
           VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 900001, 'WZ0',
                   '2026-07-21T10:00:00Z',
                   '88888888-8888-4888-8888-888888888888')"""
    )
    # And a genuinely provenance-free row, e.g. pre-M4 or insert-only backfill.
    conn.execute(
        """INSERT INTO public.client_trips
           (client_id, provider_trip_id, registration, end_timestamp)
           VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 900000, 'WZ0',
                   '2026-07-21T10:00:00Z')"""
    )
    conn.commit()
    _apply(conn, CLIENT_MIGRATIONS / CLIENT_MIGRATION)
    conn.commit()
    _check("EXPAND applies to a live post-M4 table", True)

    row = conn.execute(
        """SELECT first_seen_request_id::text,
                  first_seen_response_received_at_utc
             FROM public.client_trips WHERE provider_trip_id = 900001"""
    ).fetchone()
    _check(
        "the post-M4 half-pair survives, identity intact and instant NULL",
        row[0] == "88888888-8888-4888-8888-888888888888" and row[1] is None,
        f"{row}",
    )
    _check(
        "the provenance-free row survives with both halves NULL",
        conn.execute(
            """SELECT first_seen_request_id, first_seen_response_received_at_utc
                 FROM public.client_trips WHERE provider_trip_id = 900000"""
        ).fetchone() == (None, None),
    )
    typ = conn.execute(
        """SELECT data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name='client_trips'
              AND column_name='first_seen_response_received_at_utc'"""
    ).fetchone()[0]
    _check("the column is TIMESTAMPTZ", typ == "timestamp with time zone",
           f"got {typ}")

    con = conn.execute(
        """SELECT conname, convalidated FROM pg_constraint
            WHERE conrelid='public.client_trips'::regclass
              AND conname IN (%s, %s) ORDER BY conname""",
        (EXPAND_CONSTRAINT, STRICT_CONSTRAINT),
    ).fetchall()
    names = {c[0] for c in con}
    _check(
        "EXPAND installs the one-directional constraint",
        EXPAND_CONSTRAINT in names, f"{con}",
    )
    _check(
        "EXPAND does NOT install strict bidirectional pairing",
        STRICT_CONSTRAINT not in names,
        "strict pairing during EXPAND would reject every post-M4 half-pair and "
        "every insert from the deployed writer",
    )
    _check(
        "the EXPAND constraint is deliberately NOT VALID (no scan, still enforced)",
        dict(con)[EXPAND_CONSTRAINT] is False,
        "validating it here would scan the whole table while the ADD COLUMN "
        "ACCESS EXCLUSIVE lock is still held by the same transaction",
    )

    # Re-running the migration must be a no-op, not an error.
    try:
        _apply(conn, CLIENT_MIGRATIONS / CLIENT_MIGRATION)
        conn.commit()
        _check("the migration is re-runnable", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("the migration is re-runnable", False, f"{exc}")


def test_the_expand_constraint_permits_the_transitional_shape(conn) -> None:
    """EXPAND allows identity-without-instant and forbids the reverse.

    Identity without an instant is PROVENANCE_TIMESTAMP_PENDING — exactly what the
    deployed M4 writer produces and what every existing post-M4 row looks like. An
    instant without an identity is unattributable evidence and can only come from
    a defective writer, so that direction IS forbidden from EXPAND onward.
    """
    _client_schema(conn)
    try:
        conn.execute(
            """INSERT INTO public.client_trips
               (client_id, provider_trip_id, registration, end_timestamp,
                first_seen_request_id, first_seen_response_received_at_utc)
               VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 900003, 'WZ0',
                       '2026-07-21T10:00:00Z',
                       'b454f82c-5857-4bab-8342-b7258e5cf7de', NULL)"""
        )
        conn.commit()
        _check("EXPAND ACCEPTS identity without an instant (the old writer)", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check(
            "EXPAND ACCEPTS identity without an instant (the old writer)",
            False, f"{exc}",
        )
    for label, rid, ts in (
        ("an instant without an identity", None, "2026-07-21T11:00:00Z"),
    ):
        try:
            conn.execute(
                """INSERT INTO public.client_trips
                   (client_id, provider_trip_id, registration, end_timestamp,
                    first_seen_request_id, first_seen_response_received_at_utc)
                   VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 900002, 'WZ0',
                           '2026-07-21T10:00:00Z', %s, %s)""",
                (rid, ts),
            )
            conn.rollback()
            _check(f"{label} is refused by the database", False,
                   "the row was accepted")
        except Exception:
            conn.rollback()
            _check(f"{label} is refused by the database", True)
    # Both NULL and both present remain legal in EXPAND too.
    for label, rid, ts in (
        ("neither half", None, None),
        ("both halves", "b454f82c-5857-4bab-8342-b7258e5cf7de",
         "2026-07-21T11:00:00Z"),
    ):
        try:
            conn.execute(
                """INSERT INTO public.client_trips
                   (client_id, provider_trip_id, registration, end_timestamp,
                    first_seen_request_id, first_seen_response_received_at_utc)
                   VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', %s, 'WZ0',
                           '2026-07-21T10:00:00Z', %s, %s)""",
                (900010 if rid is None else 900011, rid, ts),
            )
            conn.commit()
            _check(f"{label} is accepted", True)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            _check(f"{label} is accepted", False, f"{exc}")


def test_first_seen_survives_every_rediscovery_shape(conn) -> None:
    """DAILY re-request, WEEKLY re-request, and an end_timestamp correction."""
    _client_schema(conn)
    cid = "bd7662a5-eeb4-4614-8720-d477abfcb227"
    first_rid = "f6222a11-06ee-4e4f-8b25-302a9d963cfa"
    first_seen = "2026-07-21T12:00:00Z"

    upsert = """
        INSERT INTO public.client_trips
            (client_id, provider_trip_id, end_timestamp, registration,
             first_seen_request_id, first_seen_response_received_at_utc)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
            end_timestamp = EXCLUDED.end_timestamp,
            registration  = EXCLUDED.registration
    """
    conn.execute(upsert, (cid, 900100, "2026-07-21T11:00:00Z", "WZ1", first_rid,
                          first_seen))
    conn.commit()

    def read():
        return conn.execute(
            """SELECT end_timestamp, registration,
                      first_seen_request_id::text,
                      first_seen_response_received_at_utc
                 FROM public.client_trips
                WHERE client_id = %s AND provider_trip_id = 900100""",
            (cid,),
        ).fetchone()

    original = read()
    # 1. DAILY rediscovery: a different request, same trip.
    conn.execute(upsert, (cid, 900100, "2026-07-21T11:00:00Z", "WZ1",
                          "44444444-4444-4444-8444-444444444444",
                          "2026-07-22T02:00:00Z"))
    conn.commit()
    _check("a DAILY rediscovery leaves first-seen unchanged", read() == original,
           f"{read()} vs {original}")

    # 2. WEEKLY-style rediscovery: a much later request.
    conn.execute(upsert, (cid, 900100, "2026-07-21T11:00:00Z", "WZ1",
                          "fbfe405c-a65f-4275-898f-deb81ceb4df2",
                          "2026-08-03T00:30:00Z"))
    conn.commit()
    _check("a WEEKLY rediscovery leaves first-seen unchanged", read() == original)

    # 3. A mutable field AND end_timestamp are corrected.
    conn.execute(upsert, (cid, 900100, "2026-07-21T15:00:00Z", "WZ2",
                          "a9703d75-e616-4005-8471-bf04ee63439c",
                          "2026-08-10T00:30:00Z"))
    conn.commit()
    after = read()
    _check(
        "correcting end_timestamp and registration leaves first-seen unchanged",
        after[2] == original[2] and after[3] == original[3],
        f"{after}",
    )
    _check(
        "and the mutable fields did move",
        after[0] != original[0] and after[1] == "WZ2",
    )
    lag_before = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=original[3], end_timestamp=original[0],
    )
    lag_after = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=after[3], end_timestamp=after[0],
    )
    _check(
        "the derived lag tracked the correction: +1 h became -3 h",
        lag_before == 3600 and lag_after == -3 * 3600,
        f"{lag_before} -> {lag_after}",
    )


def test_platform_slice_relation(conn) -> None:
    import psycopg

    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in ("008_workflow_a_control_plane.sql", "010_add_client_code.sql"):
        try:
            _apply(conn, MIGRATIONS / name)
        except Exception:
            conn.rollback()
    _apply(conn, MIGRATIONS / PLATFORM_MIGRATION)
    conn.commit()
    _check("063 applies onto the control plane", True)

    try:
        _apply(conn, MIGRATIONS / PLATFORM_MIGRATION)
        conn.commit()
        _check("063 is re-runnable", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("063 is re-runnable", False, f"{exc}")

    cid = "bd7662a5-eeb4-4614-8720-d477abfcb227"
    conn.execute(
        """INSERT INTO workflow_a_control.client_account
          (client_id,client_code,client_name,provider_type,provider_base_url,
           provider_basic_auth_username,provider_basic_auth_password_secret_ref,
           client_db_host,client_db_port,client_db_name,client_db_user,
           client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
           enabled)
          VALUES (%s,'TST00001','T','telematics','https://example.invalid','u','R',
                  '127.0.0.1',5432,'db','u','R','public','sp',true)
          ON CONFLICT (client_id) DO NOTHING""",
        (cid,),
    )
    conn.commit()

    g = m6_weekly_guarantee_seconds()
    base = {
        "client_id": cid, "client_code": "TST00001",
        "trip_end_date": date(2026, 7, 21),
        "trips_total": 3, "trips_with_provenance": 2,
        "lag_p50_seconds": 100, "lag_p90_seconds": 200,
        "lag_p95_seconds": 200, "lag_max_seconds": 200, "lag_min_seconds": 100,
        "weekly_guarantee_seconds": g, "recompute_horizon_days": 18,
        "bucket_negative": 0, "bucket_under_6h": 2, "bucket_6h_to_24h": 0,
        "bucket_1d_to_3d": 0, "bucket_3d_to_7d": 0,
        "bucket_7d_to_weekly_guarantee": 0,
        "bucket_weekly_guarantee_to_15d": 0, "bucket_over_15d": 0,
        "discovered_daily": 2, "discovered_weekly_reconciliation": 0,
        "discovered_monthly_reconciliation": 0, "discovered_unattributed": 0,
    }

    def insert(**overrides):
        row = dict(base, **overrides)
        cols = ", ".join(row)
        marks = ", ".join(["%s"] * len(row))
        sets = ", ".join(
            f"{k}=EXCLUDED.{k}" for k in row
            if k not in ("client_id", "trip_end_date")
        )
        conn.execute(
            f"""INSERT INTO workflow_a_control.trip_delivery_lag_daily ({cols})
                VALUES ({marks})
                ON CONFLICT (client_id, trip_end_date) DO UPDATE SET {sets}""",
            tuple(row.values()),
        )

    insert()
    conn.commit()
    _check("a coherent slice is accepted", True)

    # Idempotence: the same slice twice must leave one row with the same metrics.
    insert()
    conn.commit()
    n = conn.execute(
        "SELECT count(*) FROM workflow_a_control.trip_delivery_lag_daily"
    ).fetchone()[0]
    _check("re-upserting a slice leaves exactly one row", n == 1, f"{n}")

    # A recompute that finds fewer trips must REPLACE, not accumulate.
    insert(trips_total=1, trips_with_provenance=1, bucket_under_6h=1,
           discovered_daily=1, lag_p50_seconds=100, lag_p90_seconds=100,
           lag_p95_seconds=100, lag_max_seconds=100, lag_min_seconds=100)
    conn.commit()
    got = conn.execute(
        """SELECT trips_total, trips_with_provenance, bucket_under_6h
             FROM workflow_a_control.trip_delivery_lag_daily"""
    ).fetchone()
    _check(
        "a recompute REPLACES the metrics rather than merging them",
        got == (1, 1, 1),
        f"{got} — a counter would have accumulated",
    )

    for label, overrides in (
        ("buckets that do not sum to the provenance count",
         {"bucket_under_6h": 1}),
        ("attribution that does not sum to the provenance count",
         {"discovered_daily": 1}),
        ("provenance exceeding the total",
         {"trips_total": 1, "trips_with_provenance": 2, "bucket_under_6h": 2,
          "discovered_daily": 2}),
        ("percentiles present on an empty sample",
         {"trips_with_provenance": 0, "bucket_under_6h": 0,
          "discovered_daily": 0}),
        ("non-monotone percentiles",
         {"lag_p50_seconds": 500}),
        ("a zero recompute horizon", {"recompute_horizon_days": 0}),
    ):
        try:
            insert(trip_end_date=date(2026, 7, 22), **overrides)
            conn.rollback()
            _check(f"{label} is refused", False, "it was accepted")
        except Exception:
            conn.rollback()
            _check(f"{label} is refused", True)

    # An empty slice with no percentiles is legal and must be storable.
    try:
        insert(trip_end_date=date(2026, 7, 23), trips_total=0,
               trips_with_provenance=0, bucket_under_6h=0, discovered_daily=0,
               lag_p50_seconds=None, lag_p90_seconds=None,
               lag_p95_seconds=None, lag_max_seconds=None,
               lag_min_seconds=None)
        conn.commit()
        _check("an empty slice with NULL percentiles is accepted", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("an empty slice with NULL percentiles is accepted", False, f"{exc}")


def test_the_slice_stays_computable_after_the_raw_evidence_is_pruned(conn) -> None:
    """The retention boundary, made explicit without pruning anything real.

    The trip row keeps its own observation instant, so the distribution survives.
    Only the run_type attribution is lost, and it degrades to `unattributed`
    rather than to a wrong role — which is exactly why the aggregation resolves
    and STORES attribution while the evidence is still fresh.
    """
    _client_schema(conn)
    cid = "bd7662a5-eeb4-4614-8720-d477abfcb227"
    conn.execute(
        """INSERT INTO public.client_trips
           (client_id, provider_trip_id, registration, end_timestamp,
            first_seen_request_id, first_seen_response_received_at_utc)
           VALUES (%s, 900200, 'WZ0', '2026-07-21T10:00:00Z',
                   'f2da1f95-33d4-4ea3-8e5d-c22bbd7e2357',
                   '2026-07-22T10:00:00Z')""",
        (cid,),
    )
    conn.commit()
    row = conn.execute(
        """SELECT end_timestamp, first_seen_response_received_at_utc,
                  first_seen_request_id::text
             FROM public.client_trips WHERE provider_trip_id = 900200"""
    ).fetchone()

    # There is deliberately no provider_request_log row for that UUID here: this
    # is the post-prune state.
    lag = observed_delivery_lag_seconds(
        first_seen_response_received_at_utc=row[1], end_timestamp=row[0],
    )
    _check(
        "the lag is still computable with no resolvable request row",
        lag == SECONDS_PER_DAY,
        f"{lag}",
    )
    s = build_daily_slice(
        trip_end_date=date(2026, 7, 21),
        observations=[TripObservation(
            provider_trip_id=900200, end_timestamp=row[0],
            first_seen_response_received_at_utc=row[1],
            first_seen_run_type=None,
        )],
        weekly_guarantee_seconds=m6_weekly_guarantee_seconds(),
    )
    _check(
        "it still aggregates, and degrades to unattributed rather than to DAILY",
        s.trips_with_provenance == 1
        and s.discovery[DISCOVERY_UNATTRIBUTED] == 1
        and s.discovery["discovered_daily"] == 0,
        f"{s.discovery}",
    )


# ===========================================================================
# EXPAND-CONTRACT — the migration shape, the rollout, and the closure gate
# ===========================================================================

def test_the_expand_migration_shape_is_lock_safe() -> None:
    """A static contract, so the removed work cannot creep back in.

    `scripts/apply_client_business_migrations.py` runs a whole client migration
    file inside ONE transaction, so the ACCESS EXCLUSIVE lock taken by
    `ADD COLUMN` is held until the file ends. Anything scanning or building after
    it therefore runs with the table fully locked. This asserts that 048 contains
    only catalog-only work.
    """
    sql = (CLIENT_MIGRATIONS / CLIENT_MIGRATION).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    upper = body.upper()

    _check(
        "048 adds the column",
        "ADD COLUMN IF NOT EXISTS FIRST_SEEN_RESPONSE_RECEIVED_AT_UTC" in upper,
    )
    _check(
        "048 contains no VALIDATE CONSTRAINT",
        "VALIDATE CONSTRAINT" not in upper,
        "a validation scan would run under the inherited ACCESS EXCLUSIVE lock; "
        "it belongs to the CONTRACT tool, which owns its own transactions",
    )
    _check(
        "048 contains no CREATE INDEX",
        "CREATE INDEX" not in upper,
        "a non-concurrent index build would run under the same lock, and "
        "CREATE INDEX CONCURRENTLY is forbidden inside a transaction block",
    )
    _check(
        "048 contains no CONCURRENTLY (which this runner cannot support)",
        "CONCURRENTLY" not in upper,
    )
    _check(
        "048 declares the EXPAND constraint NOT VALID",
        EXPAND_CONSTRAINT in body and "NOT VALID" in upper,
    )
    _check(
        "048 never installs strict bidirectional pairing",
        STRICT_CONSTRAINT not in body,
        "that is the CONTRACT step and must not be reachable by the ordinary "
        "migration runner",
    )
    # The word "UPDATE" legitimately appears inside the COMMENT ON text (which
    # explains why the column is absent from `DO UPDATE SET`). What must not
    # appear is an UPDATE statement against the table.
    _check(
        "048 contains no UPDATE statement — it never imputes a timestamp",
        "UPDATE PUBLIC.CLIENT_TRIPS" not in upper
        and "UPDATE CLIENT_TRIPS" not in upper,
    )
    _check(
        "048 contains no DEFAULT on the new column (no table rewrite)",
        "RECEIVED_AT_UTC TIMESTAMPTZ NULL" in upper
        and "RECEIVED_AT_UTC TIMESTAMPTZ NULL DEFAULT" not in upper,
    )


def test_the_contract_closure_is_not_reachable_by_the_migration_runner() -> None:
    """No file in db/client_business/ may install strict pairing.

    That directory is applied wholesale by
    `scripts/apply_client_business_migrations.py`, so a gated file would either
    fire prematurely or, if it refused, block every later client migration behind
    it. The closure therefore lives in a reviewed operator tool.
    """
    offenders = [
        p.name for p in sorted(CLIENT_MIGRATIONS.glob("*.sql"))
        if STRICT_CONSTRAINT in p.read_text(encoding="utf-8")
    ]
    _check(
        "no client_business migration file installs the strict constraint",
        not offenders,
        f"offenders: {offenders}",
    )
    tool = (ROOT / "ops" / "close_telematics_first_seen_pair_contract.py").read_text(
        encoding="utf-8"
    )
    _check(
        "the closure tool installs it instead",
        STRICT_CONSTRAINT in tool and "VALIDATE CONSTRAINT" in tool,
    )
    _check(
        "and drops the EXPAND constraint as it does so",
        EXPAND_CONSTRAINT in tool,
    )
    _check(
        "the closure needs an explicit rollback-window assertion (G6)",
        "rollback_window_closed" in tool
        and "ROLLBACK_WINDOW_STILL_OPEN" in tool,
        "a clean G2/G3 is not consent: a brand-new client with no trips would "
        "otherwise pass every automatic check",
    )
    _check(
        "the closure needs an explicit --confirm CLOSE_CONTRACT",
        'str(args.confirm or "") != "CLOSE_CONTRACT"' in tool,
    )
    _check(
        "--check-only exists as the readiness report and never writes",
        "--check-only" in tool and "CHECK_ONLY" in tool,
    )


def test_the_enrichment_tool_never_imputes() -> None:
    tool = (
        ROOT / "ops" / "enrich_telematics_first_seen_timestamps.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(tool, "enrich")
    # Statements only. The module docstring names `synced_at` and `sync_run_id`
    # precisely to state that they are NEVER used as a fallback, and says "Every
    # UPDATE therefore carries a compare-and-swap predicate" — matching prose
    # would turn the documentation of the rule into a violation of it.
    sql = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and re.search(
            r"\b(FROM|INTO|UPDATE)\s+public\.client_trips",
            n.value, re.IGNORECASE,
        )
    ]
    _check("the enrichment SQL statements were located", bool(sql))
    writes = [s for s in sql if re.search(
        r"UPDATE\s+public\.client_trips", s, re.IGNORECASE
    )]
    _check("there is exactly one UPDATE against client_trips", len(writes) == 1,
           f"{len(writes)}")
    if writes:
        w = writes[0]
        _check(
            "the UPDATE sets only the instant column",
            "SET first_seen_response_received_at_utc" in w
            and "first_seen_request_id =" in w.split("WHERE", 1)[1],
        )
        _check(
            "the UPDATE carries the CAS predicates (identity unchanged, still pending)",
            "first_seen_request_id = %(request_id)s" in w
            and "first_seen_response_received_at_utc IS NULL" in w,
            "without them a concurrent new-writer pair could be overwritten",
        )
    for forbidden in ("synced_at", "sync_run_id", "start_timestamp", "now()"):
        _check(
            f"no enrichment SQL falls back to {forbidden}",
            not any(forbidden in s for s in sql),
        )
    _check(
        "the only source is an exact provider_request_log.request_id match",
        "WHERE request_id = ANY(%s)" in tool
        and "response_received_at_utc IS NOT NULL" in tool,
    )
    _check(
        "the platform side is opened READ ONLY",
        "READ ONLY" in tool,
    )
    _check(
        "unresolved candidates produce a distinct non-zero exit",
        "EXIT_UNRESOLVED_REMAIN" in tool,
    )
    _check(
        "the unresolved sample is bounded and carries identities only",
        "UNRESOLVED_SAMPLE_LIMIT" in tool
        and "provider_trip_id" in tool,
    )
    _check(
        "the retention deadline matches api/platform_prune.py's horizon",
        "PROVIDER_REQUEST_LOG_RETENTION_DAYS = 180" in tool
        and "PROVIDER_REQUEST_LOG_RETENTION_DAYS = 180"
        in (ROOT / "api" / "platform_prune.py").read_text(encoding="utf-8"),
        "if the prune horizon moves, the enrichment deadline must move with it",
    )


def test_onboarding_and_migration_paths_converge() -> None:
    """A client onboarded after M-LAG must not get a pre-M-LAG schema."""
    onboarding = (ROOT / "scripts" / "onboard_workflow_a_client.py").read_text(
        encoding="utf-8"
    )
    _check(
        "onboarding applies the EXPAND migration as direct DDL",
        f'"{CLIENT_MIGRATION}"' in onboarding
        or CLIENT_MIGRATION in onboarding,
    )
    _check(
        "onboarding records it in the schema_migrations baseline",
        onboarding.count(CLIENT_MIGRATION) >= 2,
        "it must appear BOTH in CLIENT_BUSINESS_DDL_FILES and in "
        "CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED, or the existing-client "
        "runner would later re-apply it and the two paths would drift",
    )
    import re as _re
    ddl_block = onboarding.split("CLIENT_BUSINESS_DDL_FILES = [", 1)[1].split("]", 1)[0]
    mark_block = onboarding.split(
        "CLIENT_BUSINESS_SCHEMA_MIGRATIONS_MARK_APPLIED = [", 1
    )[1].split("]", 1)[0]
    _check(
        "EXPAND is in the onboarding DDL list",
        CLIENT_MIGRATION in ddl_block,
    )
    _check(
        "EXPAND is in the marked-applied baseline",
        CLIENT_MIGRATION in mark_block,
    )
    _check(
        "047 is in both lists too, so the pair rolls out together",
        "047_client_trips_first_seen_request_id.sql" in ddl_block
        and "047_client_trips_first_seen_request_id.sql" in mark_block,
    )
    _check(
        "the CONTRACT ledger entry is NOT pre-marked for a new client",
        "049_client_trips_first_seen_pair_contract" not in onboarding,
        "a new client must start in EXPAND state like everyone else; claiming "
        "the contract is closed without installing the constraint would be a lie "
        "in the ledger",
    )


def test_provenance_states_are_distinguished() -> None:
    from jobs.api.telematics.delivery_lag import (
        NO_PROVENANCE,
        PROVENANCE_COMPLETE,
        PROVENANCE_TIMESTAMP_PENDING,
        provenance_state,
    )
    ts = datetime(2026, 7, 22, tzinfo=UTC)
    _check(
        "both halves -> COMPLETE",
        provenance_state(
            first_seen_request_id="a", first_seen_response_received_at_utc=ts,
        ) == PROVENANCE_COMPLETE,
    )
    _check(
        "identity only -> TIMESTAMP_PENDING, not NO_PROVENANCE",
        provenance_state(
            first_seen_request_id="a", first_seen_response_received_at_utc=None,
        ) == PROVENANCE_TIMESTAMP_PENDING,
        "conflating them would make a transitional slice look permanently "
        "incomplete and hide that enrichment can still fix it",
    )
    _check(
        "neither half -> NO_PROVENANCE",
        provenance_state(
            first_seen_request_id=None, first_seen_response_received_at_utc=None,
        ) == NO_PROVENANCE,
    )
    _check(
        "a blank identity is treated as absent, not as a pending marker",
        provenance_state(
            first_seen_request_id="   ", first_seen_response_received_at_utc=None,
        ) == NO_PROVENANCE,
    )

    end = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    s = build_daily_slice(
        trip_end_date=date(2026, 7, 21),
        observations=[
            TripObservation(
                provider_trip_id=1, end_timestamp=end,
                first_seen_response_received_at_utc=end + timedelta(hours=1),
                first_seen_request_id="r1", first_seen_run_type="DAILY",
            ),
            # PENDING: the M4 writer saw it, the instant is not copied yet.
            TripObservation(
                provider_trip_id=2, end_timestamp=end,
                first_seen_response_received_at_utc=None,
                first_seen_request_id="r2",
            ),
            TripObservation(
                provider_trip_id=3, end_timestamp=end,
                first_seen_response_received_at_utc=None,
                first_seen_request_id="r3",
            ),
            # Genuinely no provenance.
            TripObservation(
                provider_trip_id=4, end_timestamp=end,
                first_seen_response_received_at_utc=None,
            ),
        ],
        weekly_guarantee_seconds=m6_weekly_guarantee_seconds(),
    )
    _check(
        "the slice counts the two PENDING rows separately from the unprovenanced one",
        s.trips_total == 4 and s.trips_with_provenance == 1
        and s.trips_provenance_pending == 2,
        f"total={s.trips_total} with={s.trips_with_provenance} "
        f"pending={s.trips_provenance_pending}",
    )
    _check(
        "pending rows stay out of the buckets and the attribution",
        sum(s.buckets.values()) == 1 and sum(s.discovery.values()) == 1,
    )
    _check(
        "as_row() carries the pending count to the slice relation",
        s.as_row()["trips_provenance_pending"] == 2,
    )


def test_the_release_preflight_declares_expand_or_contract() -> None:
    """New code + missing 048 must fail closed; either pairing STATE is enough.

    The declaration became a two-state bridge when the CONTRACT closure was made
    deployable: a release pinned to EXPAND alone stops being activatable the
    instant the closure drops that constraint, and one pinned to the strict
    constraint alone cannot be activated until the closure creates it. Both
    states are still exact — name, canonical definition and validation status —
    so nothing is loosened; the requirement simply spans the DDL boundary.
    """
    data = json.loads((ROOT / "db" / "schema_requirements.json").read_text())
    entries = {r["migration"]: r for r in data["requirements"]}
    _check("048 is declared", CLIENT_MIGRATION in entries)
    rel = entries[CLIENT_MIGRATION]["relations"][0]
    _check(
        "no pairing constraint is declared unconditionally",
        not rel.get("constraints"),
        "an unconditional pairing requirement is exactly what deadlocks the "
        "transition, in whichever direction it is pinned",
    )
    groups = {g["state"]: g["constraints"] for g in
              rel.get("constraint_alternatives", [])}
    _check(
        "exactly two states are declared: EXPAND and CONTRACT",
        sorted(groups) == ["CONTRACT", "EXPAND"], str(sorted(groups)),
    )

    expand = (groups.get("EXPAND") or [{}])[0]
    _check(
        "the EXPAND state names the one-directional constraint",
        expand.get("name") == EXPAND_CONSTRAINT, str(expand.get("name")),
    )
    _check(
        "declared validated:false, matching the NOT VALID migration",
        expand.get("validated") is False,
        "the gate rejects a constraint that exists but is NOT VALID unless the "
        "requirement says so; 048 leaves it NOT VALID on purpose",
    )
    _check(
        "with its canonical definition, so drift is still caught",
        "first_seen_response_received_at_utc IS NULL" in expand.get("definition", "")
        and "NOT VALID" in expand.get("definition", ""),
    )

    contract = (groups.get("CONTRACT") or [{}])[0]
    _check(
        "the CONTRACT state names the strict pairing constraint",
        contract.get("name") == STRICT_CONSTRAINT, str(contract.get("name")),
    )
    _check(
        "and requires it VALIDATED, so an interrupted closure is not accepted",
        contract.get("validated") is True,
    )
    _check(
        "with the exact symmetric definition, not a one-directional lookalike",
        " ".join(contract.get("definition", "").split())
        == "CHECK (((first_seen_request_id IS NULL) "
           "= (first_seen_response_received_at_utc IS NULL)))",
        contract.get("definition", ""),
    )
    _check(
        "the declared column is nullable",
        rel["columns"][0]["nullable"] is True,
    )


# ===========================================================================
# EXPAND-CONTRACT — PostgreSQL
# ===========================================================================

OLD_WRITER_INSERT = """
    INSERT INTO public.client_trips
        (client_id, provider_trip_id, registration, end_timestamp,
         record_id, synced_at, sync_run_id, first_seen_request_id)
    VALUES (%s, %s, %s, %s, %s, now(), %s, %s)
    ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
        end_timestamp = EXCLUDED.end_timestamp,
        registration  = EXCLUDED.registration,
        synced_at     = EXCLUDED.synced_at,
        sync_run_id   = EXCLUDED.sync_run_id
"""

NEW_WRITER_INSERT = """
    INSERT INTO public.client_trips
        (client_id, provider_trip_id, registration, end_timestamp,
         record_id, synced_at, sync_run_id, first_seen_request_id,
         first_seen_response_received_at_utc)
    VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s)
    ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
        end_timestamp = EXCLUDED.end_timestamp,
        registration  = EXCLUDED.registration,
        synced_at     = EXCLUDED.synced_at,
        sync_run_id   = EXCLUDED.sync_run_id
"""

CID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
RUN = "e8d95748-107a-4eb9-8f7f-ab2901c84c97"


def test_the_old_m4_writer_still_works_against_expand_schema(conn) -> None:
    """The key compatibility requirement, and therefore rollback safety."""
    _client_schema(conn)
    try:
        conn.execute(
            OLD_WRITER_INSERT,
            (CID, 910001, "WZ1", "2026-07-21T10:00:00Z",
             "849d564b-958c-445c-8117-78c7b0fbdd96", RUN,
             "b1771622-0e33-4d68-884f-2bd827b8618e"),
        )
        conn.commit()
        _check("the M4-era INSERT succeeds on the EXPAND schema", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("the M4-era INSERT succeeds on the EXPAND schema", False, f"{exc}")
        return

    row = conn.execute(
        """SELECT first_seen_request_id::text,
                  first_seen_response_received_at_utc
             FROM public.client_trips WHERE provider_trip_id = 910001"""
    ).fetchone()
    _check(
        "it lands in the PROVENANCE_TIMESTAMP_PENDING state",
        row[0] == "b1771622-0e33-4d68-884f-2bd827b8618e" and row[1] is None,
        f"{row}",
    )
    # And its own re-upsert keeps working, which is what a rollback period needs.
    try:
        conn.execute(
            OLD_WRITER_INSERT,
            (CID, 910001, "WZ2", "2026-07-21T11:00:00Z",
             "849d564b-958c-445c-8117-78c7b0fbdd96", RUN,
             "a925994e-a20e-4134-8d48-3cf3e61d1dfa"),
        )
        conn.commit()
        _check("the M4-era re-upsert also succeeds", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("the M4-era re-upsert also succeeds", False, f"{exc}")


def _seed_platform_evidence(
    conn, *, request_id: str, received_at: str, page: int = 1,
) -> None:
    """`page` varies so two evidence rows can share one platform run.

    `uq_provider_request_log_identity` is
    (platform_run_id, endpoint, sub_window_index, page), so a caller seeding a
    second request under the same run must move one of those.
    """
    conn.execute(
        """INSERT INTO workflow_a_control.provider_request_log
           (request_id, status, platform_run_id, endpoint,
            effective_window_start_ts, effective_window_end_ts,
            sub_window_index, covers_from_ts, covers_to_ts,
            requested_from_ts, requested_to_ts, wire_start_value,
            wire_end_value, page, request_started_at_utc,
            response_received_at_utc, http_status, row_count)
           VALUES (%s,'PENDING',%s,'/trips',
                   '2026-07-19T02:00:00Z','2026-07-22T02:00:00Z',
                   1,'2026-07-19T02:00:00Z','2026-07-21T02:00:00Z',
                   '2026-07-19T02:00:00Z','2026-07-21T01:59:59Z','w1','w2',%s,
                   '2026-07-22T02:00:00Z',%s,200,10)
           ON CONFLICT (request_id) DO NOTHING""",
        (request_id, RUN, page, received_at),
    )
    conn.commit()


def test_historical_enrichment_semantics(conn) -> None:
    """Exact resolution, no-op, fail-closed, idempotent, CAS-safe.

    Exercises the tool's own SQL and resolver against a real database, driving the
    same statements the tool issues rather than a paraphrase of them.
    """
    import ops.enrich_telematics_first_seen_timestamps as enrich

    # Platform side. 061 has a preflight that RAISES unless the run-history and
    # coverage relations exist, so the whole prerequisite chain is applied rather
    # than the three files this test names directly — a swallowed preflight would
    # leave the evidence table absent and the test would fail far from the cause.
    import ops.tests_manual.test_telematics_m5_multi_cadence_identity_postgres as _m5

    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in _m5.PREREQUISITE_MIGRATIONS:
        _apply(conn, MIGRATIONS / name)
    conn.commit()
    _check(
        "the platform evidence relation exists for the enrichment test",
        conn.execute(
            "SELECT to_regclass('workflow_a_control.provider_request_log')"
        ).fetchone()[0] is not None,
    )
    resolvable = "1997cb55-5c2a-4938-872c-a933777ac938"
    _seed_platform_evidence(
        conn, request_id=resolvable, received_at="2026-07-22T10:00:00Z",
    )

    # Client side: one pending resolvable, one pending unresolvable, one already
    # complete, one with no provenance at all.
    _client_schema(conn)
    conn.execute(
        OLD_WRITER_INSERT,
        (CID, 920001, "WZ1", "2026-07-21T10:00:00Z",
         "fad7d6fb-c2b0-4d76-8d74-69f8fc7aae58", RUN, resolvable),
    )
    conn.execute(
        OLD_WRITER_INSERT,
        (CID, 920002, "WZ1", "2026-07-21T10:00:00Z",
         "d7908c0c-4585-4a63-8e0d-c0019f3913be", RUN,
         "8bb374da-77e9-4298-89e9-3c8d70b2d08b"),  # no evidence row
    )
    conn.execute(
        NEW_WRITER_INSERT,
        (CID, 920003, "WZ1", "2026-07-21T10:00:00Z",
         "e5b48446-3617-408c-8f8e-bbc9671f534f", RUN, resolvable,
         "2026-07-22T09:00:00Z"),
    )
    conn.execute(
        """INSERT INTO public.client_trips
           (client_id, provider_trip_id, registration, end_timestamp)
           VALUES (%s, 920004, 'WZ1', '2026-07-21T10:00:00Z')""",
        (CID,),
    )
    conn.commit()

    from psycopg.rows import dict_row as _dr

    with conn.cursor(row_factory=_dr) as cur:
        cur.execute(enrich.CANDIDATE_SQL)
        candidates = [dict(r) for r in cur.fetchall()]
    ids = {int(c["provider_trip_id"]) for c in candidates}
    _check(
        "the candidate set is exactly the pending rows",
        ids == {920001, 920002},
        f"{sorted(ids)} — a complete row or a no-provenance row must not be "
        "selected, which is what makes a re-run a no-op",
    )

    with conn.cursor(row_factory=_dr) as cur:
        instants = enrich.resolve_instants(
            cur, request_ids=sorted({c["first_seen_request_id"] for c in candidates}),
        )
    _check(
        "only the exactly-matching request resolves",
        set(instants) == {resolvable}, f"{sorted(instants)}",
    )

    # Enrich the resolvable one through the tool's own CAS statement.
    with conn.cursor() as cur:
        cur.execute(enrich.ENRICH_SQL, {
            "received_at": instants[resolvable], "client_id": CID,
            "provider_trip_id": 920001, "request_id": resolvable,
        })
        first_rowcount = cur.rowcount
    conn.commit()
    _check("the resolvable candidate is enriched", first_rowcount == 1)

    got = conn.execute(
        """SELECT first_seen_response_received_at_utc FROM public.client_trips
            WHERE provider_trip_id = 920001"""
    ).fetchone()[0]
    _check(
        "the stored instant is the exact provider response instant",
        got == datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
        f"{got}",
    )

    # Idempotency: the same statement again matches nothing.
    with conn.cursor() as cur:
        cur.execute(enrich.ENRICH_SQL, {
            "received_at": datetime(2030, 1, 1, tzinfo=UTC), "client_id": CID,
            "provider_trip_id": 920001, "request_id": resolvable,
        })
        second_rowcount = cur.rowcount
    conn.commit()
    _check(
        "re-running the enrichment is a no-op, not a rewrite",
        second_rowcount == 0,
        "the CAS `instant IS NULL` predicate is what prevents the rewrite",
    )
    _check(
        "and the value did not move",
        conn.execute(
            """SELECT first_seen_response_received_at_utc FROM public.client_trips
                WHERE provider_trip_id = 920001"""
        ).fetchone()[0] == got,
    )

    # The unresolvable one is untouched — never imputed.
    _check(
        "the unresolvable candidate is left NULL, never imputed",
        conn.execute(
            """SELECT first_seen_response_received_at_utc FROM public.client_trips
                WHERE provider_trip_id = 920002"""
        ).fetchone()[0] is None,
    )
    # The already-complete row is untouched.
    _check(
        "the already-complete row keeps the writer's instant",
        conn.execute(
            """SELECT first_seen_response_received_at_utc FROM public.client_trips
                WHERE provider_trip_id = 920003"""
        ).fetchone()[0] == datetime(2026, 7, 22, 9, 0, tzinfo=UTC),
    )

    # RACE: the new writer completes a pair after candidate selection.
    conn.execute(
        NEW_WRITER_INSERT,
        (CID, 920005, "WZ1", "2026-07-21T10:00:00Z",
         "35302ca6-3d97-4cf0-8fcb-d063baab7fbb", RUN, resolvable,
         "2026-07-22T08:00:00Z"),
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(enrich.ENRICH_SQL, {
            "received_at": instants[resolvable], "client_id": CID,
            "provider_trip_id": 920005, "request_id": resolvable,
        })
        raced = cur.rowcount
    conn.commit()
    _check(
        "a row completed by the new writer mid-run is NOT overwritten",
        raced == 0
        and conn.execute(
            """SELECT first_seen_response_received_at_utc FROM public.client_trips
                WHERE provider_trip_id = 920005"""
        ).fetchone()[0] == datetime(2026, 7, 22, 8, 0, tzinfo=UTC),
    )
    # RACE: the identity changed since selection.
    with conn.cursor() as cur:
        cur.execute(enrich.ENRICH_SQL, {
            "received_at": instants[resolvable], "client_id": CID,
            "provider_trip_id": 920002, "request_id": resolvable,
        })
        wrong_identity = cur.rowcount
    conn.rollback()
    _check(
        "a candidate whose identity does not match is not written",
        wrong_identity == 0,
        "the CAS binds the identity observed at selection time",
    )


def test_contract_readiness_gate_and_closure(conn) -> None:
    import ops.close_telematics_first_seen_pair_contract as closure
    from psycopg.rows import dict_row as _dr

    _client_schema(conn)
    # One pending row -> not ready.
    conn.execute(
        OLD_WRITER_INSERT,
        (CID, 930001, "WZ1", "2026-07-21T10:00:00Z",
         "b880668a-7fdf-44ac-85f3-7fe7fdd6e647", RUN,
         "4adaf68d-8ea1-48ed-8fe6-a02dd23f34e8"),
    )
    conn.commit()
    with conn.cursor(row_factory=_dr) as cur:
        state = closure.readiness(cur)
    _check(
        "the gate reports NOT ready while a pending row exists",
        state["ready"] is False
        and state["gates"]["G2_zero_pending_rows"] is False
        and int(state["pending_rows"]) == 1,
        f"{state['gates']}",
    )
    _check(
        "and it reports the counts an operator needs",
        int(state["complete_pairs"]) == 0 and int(state["total_rows"]) == 1
        and state["gates"]["G1_expand_column_present"] is True
        # G4 is exact, not by name: a same-named constraint carrying a different
        # expression proves the database did not go through THIS expand phase.
        and state["gates"]["G4_expand_constraint_exact"] is True
        and state["gates"]["G5_strict_constraint_absent"] is True,
        f"{state}",
    )
    _check(
        "and it classifies the client as OPEN, with the ledger not yet written",
        state["contract_state"] == closure.STATE_OPEN
        and state["ledger_recorded"] is False,
        f"{state.get('contract_state')}",
    )

    # Enrich it -> ready.
    conn.execute(
        """UPDATE public.client_trips
              SET first_seen_response_received_at_utc = '2026-07-22T10:00:00Z'
            WHERE provider_trip_id = 930001"""
    )
    conn.commit()
    with conn.cursor(row_factory=_dr) as cur:
        state = closure.readiness(cur)
    _check("the gate reports READY once nothing is pending", state["ready"] is True,
           f"{state['gates']}")

    # Close it.
    result = closure.close_contract(conn)
    _check("closure reports a validated strict constraint",
           result["strict_constraint_validated"] is True)
    con = conn.execute(
        """SELECT conname, convalidated FROM pg_constraint
            WHERE conrelid='public.client_trips'::regclass
              AND conname IN (%s, %s)""",
        (EXPAND_CONSTRAINT, STRICT_CONSTRAINT),
    ).fetchall()
    names = dict(con)
    _check("the strict constraint is installed and VALIDATED",
           names.get(STRICT_CONSTRAINT) is True, f"{con}")
    _check("the EXPAND constraint is gone", EXPAND_CONSTRAINT not in names)
    _check(
        "the closure is recorded in the ledger",
        conn.execute(
            "SELECT count(*) FROM public.schema_migrations WHERE filename = %s",
            (closure.LEDGER_ENTRY,),
        ).fetchone()[0] == 1,
    )
    _check(
        "the ledger entry is not a real file, so the runner cannot re-apply it",
        not (CLIENT_MIGRATIONS / closure.LEDGER_ENTRY).exists()
        and closure.LEDGER_ENTRY.endswith(".tool"),
    )
    # Rerunning the tool against a closed client must be a no-op, and must NOT
    # be the old "the strict constraint exists, so we are done" shortcut — that
    # shortcut is what made an interrupted closure unfinishable. The dedicated
    # state-machine evidence lives in
    # `ops/tests_manual/test_first_seen_pair_contract_closure_postgres.py`.
    with conn.cursor(row_factory=_dr) as cur:
        state = closure.readiness(cur)
    _check(
        "a closed client classifies as CLOSED with its ledger entry",
        state["contract_state"] == closure.STATE_CLOSED
        and state["ledger_recorded"] is True,
        f"{state.get('contract_state')}",
    )
    plan = closure.plan_action(state, execute=True)
    _check(
        "and a rerun plans nothing at all",
        plan["result"] == "ALREADY_CLOSED" and plan["action"] == closure.ACTION_NONE,
        f"{plan}",
    )

    # After closure the OLD writer is intentionally locked out.
    try:
        conn.execute(
            OLD_WRITER_INSERT,
            (CID, 930002, "WZ1", "2026-07-21T10:00:00Z",
             "07f1e8a6-f1eb-4bc6-8aad-29ccbc5c4aed", RUN,
             "88ec9421-3ddc-41f8-8763-81e7763d9332"),
        )
        conn.rollback()
        _check(
            "after CONTRACT the M4-era writer is refused — rollback window closed",
            False, "the old-style insert was accepted",
        )
    except Exception:
        conn.rollback()
        _check(
            "after CONTRACT the M4-era writer is refused — rollback window closed",
            True,
        )
    # And the new writer still works.
    try:
        conn.execute(
            NEW_WRITER_INSERT,
            (CID, 930003, "WZ1", "2026-07-21T10:00:00Z",
             "e82efd9f-2058-4767-8b2f-1217ddc51414", RUN,
             "a98d81f4-3592-4fa3-8147-fe407500a5c9", "2026-07-22T10:00:00Z"),
        )
        conn.commit()
        _check("after CONTRACT the M-LAG writer still works", True)
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _check("after CONTRACT the M-LAG writer still works", False, f"{exc}")

    # Closure is idempotent at the gate level: G5 now fails, reported as
    # ALREADY_CLOSED by the runner rather than attempted twice.
    with conn.cursor(row_factory=_dr) as cur:
        state = closure.readiness(cur)
    _check(
        "a second closure is refused by G5 rather than re-run",
        state["gates"]["G5_strict_constraint_absent"] is False,
    )


# ===========================================================================

def main() -> int:
    print("=== pure ===")
    test_the_lag_formula()
    test_the_lag_changes_when_end_timestamp_is_corrected()
    test_the_guarantee_boundary_is_derived_not_hard_coded()
    test_the_recompute_horizon_follows_the_deepest_enabled_lookback()
    test_bucket_boundaries()
    test_percentiles_are_observed_values()
    test_the_slice_partitions_everything()
    test_an_empty_slice_has_no_percentiles()
    test_the_slice_is_deterministic_and_dedupes()
    test_a_moved_trip_is_self_correcting_across_two_slices()
    test_attribution_never_invents_a_role()
    test_the_trip_upsert_writes_both_halves_and_updates_neither()
    test_the_two_halves_come_from_one_mapping_entry()
    test_only_the_insert_only_backfill_is_a_no_provenance_path()
    test_the_migration_declares_its_release_requirement()
    test_the_aggregation_tool_makes_no_provider_call()
    test_no_second_lag_calculation_exists()
    test_the_expand_migration_shape_is_lock_safe()
    test_the_contract_closure_is_not_reachable_by_the_migration_runner()
    test_the_enrichment_tool_never_imputes()
    test_onboarding_and_migration_paths_converge()
    test_provenance_states_are_distinguished()
    test_the_release_preflight_declares_expand_or_contract()

    dsn = os.environ.get(ENV, "").strip()
    if not dsn:
        print(f"\nSKIP PostgreSQL checks — set {ENV} to a disposable database")
    else:
        from ops.tests_manual.postgres_dsn_safety import (
            require_loopback_dsn_or_exit,
        )

        require_loopback_dsn_or_exit(dsn, label=ENV)
        import psycopg

        print("\n=== postgres ===")
        with psycopg.connect(dsn, autocommit=False) as conn:
            test_client_migration_is_additive_and_null_compatible(conn)
            test_the_expand_constraint_permits_the_transitional_shape(conn)
            test_first_seen_survives_every_rediscovery_shape(conn)
            test_platform_slice_relation(conn)
            test_the_slice_stays_computable_after_the_raw_evidence_is_pruned(conn)
            test_the_old_m4_writer_still_works_against_expand_schema(conn)
            test_historical_enrichment_semantics(conn)
            test_contract_readiness_gate_and_closure(conn)

    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {_failures}")
        return 1
    print("OK - M-LAG delivery-lag trace checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
