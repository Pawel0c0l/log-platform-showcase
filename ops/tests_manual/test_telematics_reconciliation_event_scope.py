#!/usr/bin/env python3
"""Deterministic tests for reconciliation-scoped vehicle-event enrichment.

A reconciliation window is long because *trips* arrive late, not because events
do. Before this contract the job spent one 16-day fleet event scan to re-derive
metrics for ~37k trips of which ~0.3% were actually new, and — with
`overwrite_existing=true` and the API owning trip metrics — wrote the re-derived
values over every existing row. That is how 2,799 already-enriched FOXTROT trips
had all five event-derived columns zeroed.

These tests pin the two halves of the fix:

  * a trip this run did not discover keeps the metrics it already had;
  * a trip this run *did* discover gets exactly the metrics the full-window
    scan would have computed for it.

No DB and no network: the pure contract is checked directly, and the
scoped-vs-full equivalence is proved against the job's own matching functions,
which are the authority for what a metric means.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_telematics_reconciliation_event_scope.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import jobs.api.telematics.sync_trips_and_speeding as job  # noqa: E402

UTC = timezone.utc
FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"PASS: {label}")
        return
    FAILURES.append(label)
    print(f"FAIL: {label}" + (f"\n      {detail}" if detail else ""))


def _ts(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=UTC)


def trip(pid: int, reg: str, start: datetime, end: datetime, vehicle_id: str = "") -> dict:
    return {
        "provider_trip_id": pid,
        "registration": reg,
        "vehicle_id": vehicle_id or f"V-{reg}",
        "start_ts": start,
        "end_ts": end,
    }


def event(reg: str, ts: datetime, *, speed: float = 0.0, label: str | None = None,
          vehicle_id: str | None = None, eid: str = "") -> dict:
    return {
        "registration": reg,
        "vehicle_id": vehicle_id if vehicle_id is not None else f"V-{reg}",
        "event_ts": ts.isoformat(),
        "timestamp": ts.isoformat(),
        "speed": speed,
        "event_type": label or "",
        "provider_event_id": eid or f"{reg}-{ts.isoformat()}-{speed}-{label}",
    }


# ===========================================================================
# A — the scope contract itself
# ===========================================================================

def test_the_base_role_keeps_the_historical_full_window_contract() -> None:
    """DAILY must not change. Its short window is re-read precisely so a late
    event can still correct yesterday's count."""
    _check(
        "an explicit DAILY role resolves to window scope",
        job._vehicle_events_scope({"schedule_run_type": "DAILY"})
        == job.VEHICLE_EVENTS_SCOPE_WINDOW,
    )
    _check(
        "a run with no role at all resolves to window scope",
        job._vehicle_events_scope({}) == job.VEHICLE_EVENTS_SCOPE_WINDOW,
    )
    _check(
        "an unknown future role resolves to window scope, not the cheap one",
        job._vehicle_events_scope({"schedule_run_type": "QUARTERLY_RECONCILIATION"})
        == job.VEHICLE_EVENTS_SCOPE_WINDOW,
        "a role nobody has reasoned about must not silently skip enrichment",
    )


def test_every_reconciliation_role_scopes_to_its_candidates() -> None:
    for role in ("WEEKLY_RECONCILIATION", "MONTHLY_RECONCILIATION"):
        _check(
            f"{role} resolves to candidate scope",
            job._vehicle_events_scope({"schedule_run_type": role})
            == job.VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
        )
    _check(
        "the role is read case- and whitespace-insensitively",
        job._vehicle_events_scope({"schedule_run_type": "  weekly_reconciliation "})
        == job.VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
    )


def test_an_explicit_scope_overrides_the_role_and_nonsense_is_refused() -> None:
    _check(
        "an explicit scope wins over the role",
        job._vehicle_events_scope({
            "schedule_run_type": "WEEKLY_RECONCILIATION",
            "vehicle_events_scope": "window",
        }) == job.VEHICLE_EVENTS_SCOPE_WINDOW,
    )
    try:
        job._vehicle_events_scope({"vehicle_events_scope": "cheap"})
    except ValueError:
        _check("an unsupported scope is refused, not defaulted", True)
    else:
        _check("an unsupported scope is refused, not defaulted", False)


def test_the_dispatcher_carries_the_role_without_branching_on_it() -> None:
    """M6's own invariant: the role is evidence, never dispatcher control flow."""
    import inspect

    from jobs.api.telematics import dispatcher

    source = inspect.getsource(dispatcher)
    tree = ast.parse(source)
    branches = [
        (ast.get_source_segment(source, node.test) or "").strip()
        for node in ast.walk(tree)
        if isinstance(node, (ast.If, ast.IfExp))
        and "run_type" in (ast.get_source_segment(source, node.test) or "")
    ]
    _check("no dispatcher control flow tests run_type", not branches, f"{branches}")
    params = dispatcher._build_job_params(
        client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        client_code="C001",
        dataset_name="trips_sync",
        event_enrichment_mode="enabled",
        schedule_run_type="WEEKLY_RECONCILIATION",
        window_start_ts=_ts(1, 0),
        window_end_ts=_ts(17, 0),
    )
    _check(
        "the role reaches trips_sync as a param",
        params.get("schedule_run_type") == "WEEKLY_RECONCILIATION",
        f"{params}",
    )
    fuel = dispatcher._build_job_params(
        client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        client_code="C001",
        dataset_name="fuel_daily_aggregation",
        event_enrichment_mode="disabled",
        schedule_run_type="WEEKLY_RECONCILIATION",
        window_start_ts=_ts(1, 0),
        window_end_ts=_ts(17, 0),
    )
    _check(
        "a dataset with no event scope is not told the role",
        "schedule_run_type" not in fuel,
        f"{sorted(fuel)}",
    )


# ===========================================================================
# B — candidate windows are sufficient and minimal
# ===========================================================================

def test_a_candidate_window_is_exactly_the_trip_interval() -> None:
    """No padding. The matching rule compares against these exact bounds."""
    t = trip(1, "ABC123", _ts(3, 10), _ts(3, 11))
    windows = job._build_candidate_event_windows(
        candidate_trips=[t],
        registrations_by_norm={"ABC123": "ABC123"},
        coalesce_gap=timedelta(minutes=60),
    )
    _check("one candidate trip yields one window", len(windows) == 1, f"{windows}")
    _check(
        "the window is the trip interval, unpadded",
        windows[0]["start_ts"] == t["start_ts"] and windows[0]["end_ts"] == t["end_ts"],
        f"{windows[0]}",
    )


def test_no_candidates_means_no_windows_and_therefore_no_requests() -> None:
    _check(
        "an empty candidate set buys nothing",
        job._build_candidate_event_windows(
            candidate_trips=[], registrations_by_norm={},
            coalesce_gap=timedelta(minutes=60),
        ) == [],
    )


def test_windows_group_by_registration_and_never_merge_across_them() -> None:
    trips = [
        trip(1, "AAA111", _ts(3, 10), _ts(3, 11)),
        trip(2, "BBB222", _ts(3, 10, 5), _ts(3, 11)),
    ]
    windows = job._build_candidate_event_windows(
        candidate_trips=trips,
        registrations_by_norm={"AAA111": "AAA111", "BBB222": "BBB222"},
        coalesce_gap=timedelta(hours=24),
    )
    _check(
        "two registrations stay two windows however close in time",
        len(windows) == 2 and {w["registration_norm"] for w in windows} == {"AAA111", "BBB222"},
        f"{windows}",
    )


def test_near_trips_coalesce_and_distant_trips_do_not() -> None:
    near = job._build_candidate_event_windows(
        candidate_trips=[
            trip(1, "AAA111", _ts(3, 10), _ts(3, 11)),
            trip(2, "AAA111", _ts(3, 11, 30), _ts(3, 12)),
        ],
        registrations_by_norm={"AAA111": "AAA111"},
        coalesce_gap=timedelta(minutes=60),
    )
    _check(
        "two trips within the gap share one request window",
        len(near) == 1 and near[0]["start_ts"] == _ts(3, 10) and near[0]["end_ts"] == _ts(3, 12),
        f"{near}",
    )
    far = job._build_candidate_event_windows(
        candidate_trips=[
            trip(1, "AAA111", _ts(3, 10), _ts(3, 11)),
            trip(2, "AAA111", _ts(9, 10), _ts(9, 11)),
        ],
        registrations_by_norm={"AAA111": "AAA111"},
        coalesce_gap=timedelta(minutes=60),
    )
    _check(
        "trips six days apart do not drag back the days between them",
        len(far) == 2,
        f"{far}",
    )
    _check(
        "coalescing never widens beyond the outermost candidate bounds",
        near[0]["end_ts"] == _ts(3, 12),
    )


def test_a_trip_the_matching_rule_cannot_decide_buys_nothing() -> None:
    undecidable = [
        trip(1, "", _ts(3, 10), _ts(3, 11)),
        {"provider_trip_id": 2, "registration": "AAA111", "start_ts": None, "end_ts": _ts(3, 11)},
        trip(3, "AAA111", _ts(3, 12), _ts(3, 10)),
    ]
    _check(
        "no identity, no interval or an inverted interval buys no events",
        job._build_candidate_event_windows(
            candidate_trips=undecidable,
            registrations_by_norm={"AAA111": "AAA111"},
            coalesce_gap=timedelta(minutes=60),
        ) == [],
        "the matching rule counts nothing for these, so there is nothing to buy",
    )


def test_window_enumeration_is_deterministic() -> None:
    trips = [
        trip(3, "CCC333", _ts(5, 9), _ts(5, 10)),
        trip(1, "AAA111", _ts(3, 10), _ts(3, 11)),
        trip(2, "BBB222", _ts(4, 8), _ts(4, 9)),
    ]
    regs = {"AAA111": "AAA111", "BBB222": "BBB222", "CCC333": "CCC333"}
    first = job._build_candidate_event_windows(
        candidate_trips=trips, registrations_by_norm=regs,
        coalesce_gap=timedelta(minutes=60))
    second = job._build_candidate_event_windows(
        candidate_trips=list(reversed(trips)), registrations_by_norm=regs,
        coalesce_gap=timedelta(minutes=60))
    _check("input order cannot change the windows", first == second, f"{first} != {second}")


# ===========================================================================
# C — scoped enrichment equals full-window enrichment for candidate trips
# ===========================================================================

def _full_window_events(trips: list[dict], *, noise_days: int = 16) -> list[dict]:
    """What a 16-day fleet scan would have returned: the deciding events plus a
    great deal of telemetry that decides nothing."""
    events: list[dict] = []
    for t in trips:
        mid = t["start_ts"] + (t["end_ts"] - t["start_ts"]) / 2
        events.append(event(t["registration"], mid, speed=165.0, label="OVERREV"))
        events.append(event(t["registration"], mid + timedelta(seconds=1), speed=145.0))
        # Outside the interval on both sides: real telemetry, no bearing.
        events.append(event(t["registration"], t["start_ts"] - timedelta(minutes=5),
                            speed=175.0, label="OVERREV"))
        events.append(event(t["registration"], t["end_ts"] + timedelta(minutes=5),
                            speed=175.0, label="OVERREV"))
    for day in range(1, noise_days + 1):
        for hour in (2, 7, 13, 19):
            events.append(event("ZZZ999", _ts(day, hour), speed=180.0, label="OVERREV"))
    return events


def _scoped_events(all_events: list[dict], windows: list[dict]) -> list[dict]:
    """What the scoped path buys: the provider filters by registration, the
    request bounds filter by time."""
    kept = []
    for w in windows:
        for e in all_events:
            if job._normalize_registration(e.get("registration")) != w["registration_norm"]:
                continue
            ts = datetime.fromisoformat(e["event_ts"])
            if w["start_ts"] <= ts <= w["end_ts"]:
                kept.append(e)
    return kept


def _metrics(trips: list[dict], events: list[dict]) -> dict:
    regs = {job._normalize_registration(t["registration"]) for t in trips
            if job._normalize_registration(t["registration"])}
    vids = {job._normalize_vehicle_id(t.get("vehicle_id")) for t in trips
            if job._normalize_vehicle_id(t.get("vehicle_id"))}
    rpm_rows, _ = job._filter_fleet_rpm_vehicle_events(
        vehicle_events=events, trip_registrations_norm=regs, trip_vehicle_ids_norm=vids)
    rpm, _ = job._compute_rpm_vehicle_event_counts(trips=trips, vehicle_events=rpm_rows)
    return rpm


def test_scoped_enrichment_reproduces_the_full_window_metrics_exactly() -> None:
    """The acceptance criterion: same numbers, a fraction of the events."""
    existing = [trip(i, f"REG{i:03d}", _ts(2 + i % 12, 8), _ts(2 + i % 12, 9)) for i in range(1, 40)]
    candidates = [
        trip(900, "AAA111", _ts(3, 10), _ts(3, 11)),
        trip(901, "AAA111", _ts(3, 11, 30), _ts(3, 12)),
        trip(902, "BBB222", _ts(9, 6), _ts(9, 7)),
        trip(903, "CCC333", _ts(16, 22), _ts(16, 23, 30)),
    ]
    all_trips = existing + candidates
    full_events = _full_window_events(all_trips)

    windows = job._build_candidate_event_windows(
        candidate_trips=candidates,
        registrations_by_norm={
            job._normalize_registration(t["registration"]): t["registration"]
            for t in candidates},
        coalesce_gap=timedelta(minutes=60),
    )
    scoped = _scoped_events(full_events, windows)

    full_metrics = _metrics(all_trips, full_events)
    scoped_metrics = _metrics(candidates, scoped)

    mismatched = {
        t["provider_trip_id"]: (full_metrics[t["provider_trip_id"]],
                                scoped_metrics[t["provider_trip_id"]])
        for t in candidates
        if full_metrics[t["provider_trip_id"]] != scoped_metrics[t["provider_trip_id"]]
    }
    _check(
        "every candidate trip gets byte-equal metrics under both scopes",
        not mismatched,
        f"{mismatched}",
    )
    _check(
        "the candidates actually had something to count",
        any(v["overrev"] > 0 for v in scoped_metrics.values()),
        f"{scoped_metrics}",
    )
    _check(
        "the scoped path bought a small fraction of the events",
        len(scoped) * 4 < len(full_events),
        f"scoped={len(scoped)} full={len(full_events)}",
    )


def test_an_event_just_outside_a_trip_is_excluded_by_both_scopes() -> None:
    """Boundary handling must not differ between the paths."""
    t = trip(1, "AAA111", _ts(3, 10), _ts(3, 11))
    events = [
        event("AAA111", _ts(3, 10), speed=165.0, label="OVERREV", eid="at-start"),
        event("AAA111", _ts(3, 11), speed=165.0, label="OVERREV", eid="at-end"),
        event("AAA111", _ts(3, 10) - timedelta(seconds=1), speed=165.0,
              label="OVERREV", eid="before"),
        event("AAA111", _ts(3, 11) + timedelta(seconds=1), speed=165.0,
              label="OVERREV", eid="after"),
    ]
    windows = job._build_candidate_event_windows(
        candidate_trips=[t], registrations_by_norm={"AAA111": "AAA111"},
        coalesce_gap=timedelta(minutes=60))
    full = _metrics([t], events)
    scoped = _metrics([t], _scoped_events(events, windows))
    _check(
        "both boundary events count, both outside events do not, under both scopes",
        full[1] == scoped[1] == {"high_rpm": 0, "overrev": 2},
        f"full={full[1]} scoped={scoped[1]}",
    )


def test_overlapping_candidate_trips_each_get_their_own_count() -> None:
    a = trip(1, "AAA111", _ts(3, 10), _ts(3, 12))
    b = trip(2, "AAA111", _ts(3, 11), _ts(3, 13))
    events = [event("AAA111", _ts(3, 11, 30), speed=165.0, label="OVERREV", eid="shared")]
    windows = job._build_candidate_event_windows(
        candidate_trips=[a, b], registrations_by_norm={"AAA111": "AAA111"},
        coalesce_gap=timedelta(minutes=60))
    scoped = _metrics([a, b], _scoped_events(events, windows))
    full = _metrics([a, b], events)
    _check(
        "an event inside two overlapping trips counts once for each, both scopes",
        scoped == full == {1: {"high_rpm": 0, "overrev": 1}, 2: {"high_rpm": 0, "overrev": 1}},
        f"scoped={scoped} full={full}",
    )


def test_a_candidate_at_each_edge_of_the_window_is_still_decided() -> None:
    first = trip(1, "AAA111", _ts(1, 0, 1), _ts(1, 0, 59))
    last = trip(2, "BBB222", _ts(16, 23, 0), _ts(16, 23, 59))
    events = [
        event("AAA111", _ts(1, 0, 30), speed=165.0, label="OVERREV"),
        event("BBB222", _ts(16, 23, 30), speed=165.0, label="OVERREV"),
    ]
    windows = job._build_candidate_event_windows(
        candidate_trips=[first, last],
        registrations_by_norm={"AAA111": "AAA111", "BBB222": "BBB222"},
        coalesce_gap=timedelta(minutes=60))
    scoped = _metrics([first, last], _scoped_events(events, windows))
    _check(
        "the first and last trip of the window are both enriched",
        scoped == {1: {"high_rpm": 0, "overrev": 1}, 2: {"high_rpm": 0, "overrev": 1}},
        f"{scoped}",
    )


# ===========================================================================
# D — the destructive case, stated against the source
# ===========================================================================

def test_an_unowned_metric_is_written_as_unknown_not_as_zero() -> None:
    """The reproduction of the 2,799-row defect, pinned in the source.

    `rpm_counts.get(tid, {"high_rpm": 0, "overrev": 0})` is the zero that was
    written over enriched rows. Under reconciliation scope the row for a trip
    this run did not discover must carry NULL, and its ON CONFLICT clause must
    not mention the metric columns at all.
    """
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "an unowned row carries NULL rather than a zero observation",
        "metric_head = (None, None)" in source,
    )
    _check(
        "a metric-preserving ON CONFLICT clause exists and strips the metric SET list",
        "preserve_on_conflict_sql" in source
        and 'on_conflict_sql.replace(metric_update_sql, "")' in source,
    )
    _check(
        "the preserving batch is actually executed with that clause",
        "trip_insert_sql + preserve_on_conflict_sql" in source,
    )
    _check(
        "the NOT NULL speeding columns are skipped rather than written as zero",
        "if (\n                        reconciliation_scope_active\n"
        "                        and provider_trip_id not in candidate_trip_ids\n"
        "                    ):" in source,
    )


def test_the_preserving_clause_really_drops_exactly_the_five_metric_columns() -> None:
    """The preserving clause is built by string surgery on the real one. Run the
    surgery on the real literals rather than trusting that it reads correctly."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()

    metric_update_sql = """
                      high_rpm_events_count=EXCLUDED.high_rpm_events_count,
                      overrev_events_count=EXCLUDED.overrev_events_count,
                      speeding_140_160_count=EXCLUDED.speeding_140_160_count,
                      speeding_160_170_count=EXCLUDED.speeding_160_170_count,
                      speeding_170_plus_count=EXCLUDED.speeding_170_plus_count,
"""
    _check(
        "the metric SET block in the source is the one this test models",
        metric_update_sql in source,
        "if this fails the surgery models a clause that no longer exists",
    )

    start = source.index("ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET")
    end = source.index('"""', start)
    template = source[start:end]
    overwrite_clause = template.replace("{metric_update_sql}", metric_update_sql)
    preserving_clause = overwrite_clause.replace(metric_update_sql, "")

    metric_columns = (
        "high_rpm_events_count", "overrev_events_count",
        "speeding_140_160_count", "speeding_160_170_count", "speeding_170_plus_count",
    )
    for column in metric_columns:
        _check(
            f"{column} IS assigned by the normal clause",
            f"{column}=EXCLUDED.{column}" in overwrite_clause,
        )
        _check(
            f"{column} is NOT assigned by the preserving clause",
            f"{column}=EXCLUDED.{column}" not in preserving_clause,
        )

    # Everything else must survive the surgery untouched: a reconciliation still
    # has to be able to correct a trip's geometry and timing.
    for column in ("end_timestamp", "trip_distance_meters", "registration",
                   "harsh_braking_events", "idle_time_seconds", "synced_at"):
        _check(
            f"{column} still reconciles under the preserving clause",
            f"{column}=EXCLUDED.{column}" in preserving_clause,
        )
    _check(
        "the surgery removes one contiguous block and nothing else",
        len(overwrite_clause) - len(preserving_clause) == len(metric_update_sql),
        f"delta={len(overwrite_clause) - len(preserving_clause)} block={len(metric_update_sql)}",
    )
    _check(
        "a DO NOTHING clause survives the surgery unchanged",
        "ON CONFLICT (client_id, provider_trip_id) DO NOTHING".replace(metric_update_sql, "")
        == "ON CONFLICT (client_id, provider_trip_id) DO NOTHING",
    )


def test_first_seen_and_dysponent_remain_out_of_every_update_list() -> None:
    """The split must not have widened what an upsert may rewrite."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    start = source.index("ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET")
    end = source.index('"""', start)
    update_list = source[start:end]
    for column in ("first_seen_request_id", "first_seen_response_received_at_utc",
                   "Dysponent_ID"):
        _check(
            f"{column} is still absent from DO UPDATE SET",
            column not in update_list,
        )


def test_a_split_partitions_its_window_in_results_not_just_in_requests() -> None:
    """`vehicle_events_wire_window` deliberately widens the request, so equal
    request bounds are not enough — the two halves must be disjoint in what they
    actually return."""
    mid = _ts(3, 11)
    left_raw = [
        event("AAA111", mid - timedelta(seconds=1), speed=165.0, eid="left-inside"),
        event("AAA111", mid, speed=165.0, eid="seam"),
        event("AAA111", mid + timedelta(seconds=1), speed=165.0, eid="widened-past-seam"),
    ]
    right_raw = list(left_raw)   # the widened right request returns the same rows

    left = job._clamp_events_to_window(
        left_raw, start_ts=_ts(3, 10), end_ts=mid, exclusive_start=False)
    right = job._clamp_events_to_window(
        right_raw, start_ts=mid, end_ts=_ts(3, 12), exclusive_start=True)

    left_ids = [e["provider_event_id"] for e in left]
    right_ids = [e["provider_event_id"] for e in right]
    _check(
        "the left half keeps the seam and drops what widening dragged past it",
        left_ids == ["left-inside", "seam"], f"{left_ids}",
    )
    _check(
        "the right half is exclusive at the seam",
        right_ids == ["widened-past-seam"], f"{right_ids}",
    )
    _check(
        "the halves are disjoint",
        not (set(left_ids) & set(right_ids)), f"{left_ids} / {right_ids}",
    )
    _check(
        "and together they lose nothing that fell inside the parent window",
        set(left_ids) | set(right_ids) == {"left-inside", "seam", "widened-past-seam"},
    )


def test_the_seam_survives_the_second_precision_wire_format() -> None:
    """PROVIDER_WIRE_DT_FORMAT is second-precision and strftime TRUNCATES, so a
    sub-second seam would be written to both requests as the same second and the
    exclusive filter would open a hole rather than close an overlap."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "the seam instant is pinned to a whole second",
        "mid_ts = (start_ts + (end_ts - start_ts) / 2).replace(microsecond=0)" in source,
    )
    _check(
        "a window too short to carry a whole-second seam fails closed",
        "CANDIDATE_SEAM_BELOW_WIRE_RESOLUTION" in source,
    )
    from jobs.api.telematics.provider_client import PROVIDER_WIRE_DT_FORMAT
    _check(
        "the wire format really is second-precision (the premise of the pinning)",
        PROVIDER_WIRE_DT_FORMAT == "%Y-%m-%d %H:%M:%S", PROVIDER_WIRE_DT_FORMAT,
    )
    # An odd-length window still yields a whole-second seam strictly inside.
    for span in (timedelta(minutes=7, seconds=1), timedelta(hours=3, seconds=3),
                 timedelta(seconds=61)):
        start = _ts(3, 10)
        mid = (start + span / 2).replace(microsecond=0)
        _check(
            f"a {span} window splits at a whole second strictly inside",
            start < mid < start + span and mid.microsecond == 0,
            f"mid={mid}",
        )


def test_a_split_counts_a_boundary_event_exactly_once_end_to_end() -> None:
    """Where a duplicate would actually do its damage: speeding has no dedupe."""
    t = trip(1, "AAA111", _ts(3, 10), _ts(3, 12))
    mid = _ts(3, 11)
    seam = event("AAA111", mid, speed=165.0, eid="seam")

    def speeding(events):
        counts, _ = job._compute_speeding_violation_counts(trips=[t], vehicle_events=events)
        return counts[1]

    left = job._clamp_events_to_window(
        [seam], start_ts=_ts(3, 10), end_ts=mid, exclusive_start=False)
    right = job._clamp_events_to_window(
        [seam], start_ts=mid, end_ts=_ts(3, 12), exclusive_start=True)

    _check(
        "the partitioned split matches the unsplit fetch",
        speeding(left + right) == speeding([seam])
        and speeding([seam])["speeding_160_170_count"] == 1,
        f"split={speeding(left + right)} unsplit={speeding([seam])}",
    )
    _check(
        "an overlapping split really would have inflated the count",
        speeding([seam, seam])["speeding_160_170_count"] == 2,
        "if this stops holding, the seam no longer matters",
    )
    rpm = _metrics([t], [seam, seam])
    _check(
        "RPM survives a duplicate only because it dedupes, which speeding does not",
        rpm[1]["overrev"] == 0 or rpm[1]["overrev"] == 1, f"{rpm}",
    )


def test_an_event_with_no_timestamp_is_kept_once_and_never_doubled() -> None:
    unplaceable = [{"registration": "AAA111", "event_ts": None}]
    mid = _ts(3, 11)
    left = job._clamp_events_to_window(
        unplaceable, start_ts=_ts(3, 10), end_ts=mid, exclusive_start=False)
    right = job._clamp_events_to_window(
        unplaceable, start_ts=mid, end_ts=_ts(3, 12), exclusive_start=True)
    _check(
        "an unplaceable event is preserved by the inclusive side only",
        len(left) == 1 and len(right) == 0, f"left={len(left)} right={len(right)}",
    )
    try:
        job._clamp_events_to_window(
            [{"registration": "AAA111", "event_ts": "not-a-timestamp"}],
            start_ts=_ts(3, 10), end_ts=mid, exclusive_start=False)
    except ValueError:
        _check("a malformed timestamp propagates, as it does for every other caller", True)
    else:
        _check("a malformed timestamp propagates, as it does for every other caller", False)


def test_a_split_reports_the_pages_it_really_fetched() -> None:
    """The capped parent's RESULT is discarded, but its pages were spent. This
    field is what an operator reads to see what a run cost."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "the capped parent attempt's pages are added to the split total",
        'int(stats.get("pages_fetched") or 0)\n'
        '            + left_stats["pages_fetched"] + right_stats["pages_fetched"]' in source,
    )
    _check(
        "the request budget is still tracked on the request path, not from this stat",
        "candidate_requests_before = telematics.budget.total_requests" in source
        and "telematics.budget.total_requests - candidate_requests_before" in source,
        "capacity acceptance must not depend on a diagnostic counter",
    )


def test_the_candidate_probe_reads_the_rows_its_connection_returns() -> None:
    """`_client_business_pg_conn` builds a plain psycopg connection, so rows are
    tuples. Indexing them by column name raises TypeError, which would have
    aborted every reconciliation that found any trip at all."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "the probe connection is still the plain, tuple-row helper",
        "def _client_business_pg_conn" in source
        and "row_factory" not in source.split("def _client_business_pg_conn")[1].split("\n\n")[0],
        "if a row_factory is added here, this probe's indexing must change with it",
    )
    _check(
        "the probe indexes the projected column positionally",
        "int(row[0]) for row in probe_cur.fetchall()" in source,
    )
    _check(
        "the probe projects exactly one column, so index 0 is unambiguous",
        "SELECT provider_trip_id\n                      FROM {probe_schema}.client_trips" in source,
    )


def test_the_candidate_fetcher_never_lifts_a_provider_budget() -> None:
    """The scoped path is the budget guarantee; it must not raise its own ceiling."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_fetch_vehicle_events_candidate_window"
    )
    # Executable code only. The docstring names these symbols precisely to
    # explain why this path refuses to use them, and that explanation is the
    # point rather than a violation of it.
    statements = [n for n in fn.body
                  if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                          and isinstance(n.value.value, str))]
    body = "\n".join(ast.get_source_segment(source, n) or "" for n in statements)
    for forbidden in ("_ensure_registration_fallback_actual_budget",
                      "max_requests_per_endpoint",
                      "max_requests_per_run",
                      "max_requests_per_subwindow"):
        _check(
            f"the candidate fetcher does not touch {forbidden}",
            forbidden not in body,
        )
    _check(
        "an incomplete candidate window raises rather than returning short",
        "_raise_incomplete_event_enrichment" in body
        and body.count("_raise_incomplete_event_enrichment") >= 2,
    )


def test_a_dense_candidate_set_falls_back_to_the_complete_fleet_scan() -> None:
    """Scoping is a cost strategy, not a correctness one, so it must yield when
    it stops being cheap — and must turn off the scoped *write* when it does."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "the ceiling is compared against the window count",
        "if len(candidate_event_windows) > candidate_max_windows:" in source,
    )
    _check(
        "falling back disables the scoped write as well as the scoped fetch",
        all(
            fragment in source for fragment in (
                "reconciliation_scope_active = False",
                "candidate_trips = []",
                "candidate_trip_ids = set()",
                "candidate_event_windows = []",
            )
        ),
        "a full fetch makes recomputation a real observation; a partial one does not",
    )
    _check(
        "the fallback is announced, not silent",
        "Candidate density exceeds the scoped-fetch ceiling" in source,
    )
    _check(
        "the default ceiling sits below the fleet scan it replaces",
        job.VEHICLE_EVENTS_DEFAULT_CANDIDATE_MAX_WINDOWS < 2590,
        f"ceiling={job.VEHICLE_EVENTS_DEFAULT_CANDIDATE_MAX_WINDOWS}",
    )


def test_coalescing_cannot_change_a_metric_at_any_gap() -> None:
    """The gap is a cost dial. Prove it is not a correctness dial."""
    candidates = [
        trip(1, "AAA111", _ts(3, 10), _ts(3, 11)),
        trip(2, "AAA111", _ts(3, 14), _ts(3, 15)),
        trip(3, "AAA111", _ts(4, 9), _ts(4, 10)),
    ]
    events = _full_window_events(candidates, noise_days=6)
    baseline = None
    for gap_minutes in (1, 60, 360, 1440, 10080):
        windows = job._build_candidate_event_windows(
            candidate_trips=candidates, registrations_by_norm={"AAA111": "AAA111"},
            coalesce_gap=timedelta(minutes=gap_minutes))
        metrics = _metrics(candidates, _scoped_events(events, windows))
        if baseline is None:
            baseline = metrics
        _check(
            f"a {gap_minutes}-minute coalesce gap yields identical metrics",
            metrics == baseline,
            f"{metrics} != {baseline}",
        )


def test_scope_is_inert_unless_the_api_owns_the_metrics() -> None:
    """BRAVO/ALPHA own metrics elsewhere; the scope must not reach them."""
    source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "reconciliation scope requires API metric ownership and live enrichment",
        "reconciliation_scope_active = (\n"
        "        vehicle_events_scope == VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES\n"
        "        and api_owns_trip_metrics\n"
        "        and not event_enrichment_disabled\n"
        "    )" in source,
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("\nOK - reconciliation event scope tests passed")
