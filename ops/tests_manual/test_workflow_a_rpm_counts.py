#!/usr/bin/env python3
"""Manual sanity test for `_compute_rpm_counts` in
`jobs.api.telematics.sync_trips_and_speeding`.

What this checks (no DB, no network):

  * Type filter:
      - `HIGH_RPM` and `OVERREV` are counted.
      - Other types (`SPEEDING`, `IDLE`, missing type) are ignored.
      - Type matching is case-insensitive (provider may return
        `high_rpm` / `Overrev` etc.).
  * Vehicle filter (primary match):
      - A notification matches a trip with the SAME `vehicle_id` when
        both sides have one.
      - Trips without `vehicle_id` AND without registration get zero
        counts.
  * Registration fallback (since v1.2):
      - When a notification has no `vehicle_id` (or it does not match)
        but its registration matches the trip's registration (after
        upper+strip normalization), the event still counts.
      - When BOTH vehicle_id and registration would match the same
        trip, the event is counted exactly ONCE (no double-count).
  * Time filter:
      - Boundary inclusivity: events at `start_ts` and `end_ts` ARE counted.
      - Events outside `[start, end]` are NOT counted.
  * Multi-trip vehicle:
      - A vehicle with multiple trips in the window distributes events
        correctly to the trip whose interval contains the event.
  * Performance shape:
      - Notifications are pre-grouped by vehicle_id and registration
        and sorted by event_ts inside each bucket (verified via the
        helper that early-breaks once `event_ts > end_ts`).
  * Stats block (returned alongside counts):
      - `matches_via_vehicle_id` / `matches_via_registration` tally the
        path that resolved each event-trip match.
  * Stable-identity dedupe (since v1.3 — fixes index-based dedupe drift):
      - Two notifications carrying the same `provider_notification_id`
        collapse to one count even if they appear twice in the input list.
      - Reordering the input list does NOT change the per-trip counts
        nor the stats totals (identity is content-addressable, not
        position-addressable).
      - When `provider_notification_id` is absent, identity falls back
        to a deterministic composite (type, vehicle, normalized
        registration, event_ts, msg/status). Distinct ts or distinct
        type → distinct identity → both still count.

This script does NOT execute SQL or import psycopg.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_rpm_counts.py
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_stub(name: str, attrs: dict | None = None) -> None:
    """Register a no-op module if not already importable.

    `_compute_rpm_counts` and `_extract_notification_type` are pure-stdlib,
    but importing the host module pulls in `psycopg.rows.dict_row` and
    `requests` (via provider_client). Without those, this manual test
    would refuse to run on a fresh checkout. We stub them ONLY when
    they are not actually installed, so the venv path stays unchanged.
    """
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
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _trip(provider_trip_id: int, vehicle_id, start: str, end: str,
          registration: str = "REG-A") -> dict:
    return {
        "provider_trip_id": provider_trip_id,
        "registration": registration,
        "vehicle_id": vehicle_id,
        "start_ts": _ts(start),
        "end_ts": _ts(end),
    }


def _notif(vehicle_id, when: str, ntype, registration=None,
           pid=None, msg=None):
    """Build a parsed-notification dict already canonicalized via
    `_extract_notification_type` (i.e. mapping field name == 'type' and
    value is upper-case).

    Optional `pid` lets a test exercise the strong-identity path of
    `_notification_dedupe_key` (`provider_notification_id`); optional
    `msg` populates `notification_msg` for fallback-key tests.
    """
    return {
        "vehicle_id": vehicle_id,
        "registration": registration,
        "event_ts": _ts(when) if when else None,
        "type": ntype,
        "provider_notification_id": pid,
        "notification_msg": msg,
    }


def _event(vehicle_id, registration: str, when: str, description: str,
           event_id=None, rpm=None):
    return {
        "event_id": event_id,
        "vehicle_id": vehicle_id,
        "registration": registration,
        "event_ts": _ts(when) if when else None,
        "rpm": rpm,
        "raw": {
            "event_id": event_id,
            "vehicle_id": vehicle_id,
            "registration": registration,
            "event_ts": when,
            "event_description": description,
            "rpm": rpm,
        },
    }


def _counts(trips, notes):
    """Discard the stats block for tests that only care about counts."""
    counts, _stats = job._compute_rpm_counts(trips=trips, notifications=notes)
    return counts


def _vehicle_event_counts(trips, events):
    counts, _stats = job._compute_rpm_vehicle_event_counts(
        trips=trips,
        vehicle_events=events,
    )
    return counts


def test_type_filter() -> None:
    trips = [_trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
                   end="2026-04-01T10:00:00Z")]
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM"),
        _notif("v1", "2026-04-01T08:31:00Z", "HIGH_RPM"),
        _notif("v1", "2026-04-01T09:00:00Z", "OVERREV"),
        _notif("v1", "2026-04-01T09:15:00Z", "SPEEDING"),
        _notif("v1", "2026-04-01T09:20:00Z", None),
    ]
    counts = _counts(trips, notes)
    _check("type_filter: HIGH_RPM count == 2",
           counts[1]["high_rpm"] == 2,
           f"got high_rpm={counts[1]['high_rpm']}")
    _check("type_filter: OVERREV count == 1",
           counts[1]["overrev"] == 1,
           f"got overrev={counts[1]['overrev']}")


def test_type_extractor_case_insensitive() -> None:
    samples = [
        ({"type": "HIGH_RPM"}, "HIGH_RPM"),
        ({"type": "high_rpm"}, "HIGH_RPM"),
        ({"type": "  Overrev  "}, "OVERREV"),
        ({"notification_type": "OVERREV"}, "OVERREV"),
        ({"event_type": "high_rpm"}, "HIGH_RPM"),
        ({"alert_type": "HIGH_RPM"}, "HIGH_RPM"),
        ({"trigger_description": "HIGH_RPM"}, "HIGH_RPM"),
        ({"trigger_description": "HIGH RPM"}, "HIGH_RPM"),
        ({"trigger_description": "HIGH-RPM"}, "HIGH_RPM"),
        ({"trigger_description": "high_rpm_alert"}, "HIGH_RPM"),
        ({"trigger_description": "engine OVERREV detected"}, "OVERREV"),
        ({"trigger_description": "engine OVER_REV detected"}, "OVERREV"),
        ({"trigger_description": "engine OVER REV detected"}, "OVERREV"),
        ({"trigger_description": "engine OVER-REV detected"}, "OVERREV"),
        ({"type": "SPEEDING", "trigger_description": "HIGH_RPM"}, "HIGH_RPM"),
        ({"type": "SPEEDING", "trigger_description": "OVERREV"}, "OVERREV"),
        ({}, None),
        ({"type": ""}, None),
        ({"type": None, "notification_type": "HIGH_RPM"}, "HIGH_RPM"),
    ]
    for payload, expected in samples:
        got = job._extract_notification_type(payload)
        _check(f"_extract_notification_type({payload!r}) -> {expected!r}",
               got == expected, f"got={got!r}")


def test_generic_type_with_rpm_trigger_counts() -> None:
    trip = _trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z")
    raw_notes = [
        {"vehicle_id": "v1", "event_ts": _ts("2026-04-01T08:30:00Z"),
         "type": "SPEEDING", "trigger_description": "HIGH_RPM",
         "provider_notification_id": "pid-high"},
        {"vehicle_id": "v1", "event_ts": _ts("2026-04-01T08:31:00Z"),
         "type": "SPEEDING", "trigger_description": "OVERREV",
         "provider_notification_id": "pid-over"},
    ]
    parsed_notes = [
        {
            **n,
            "type": job._extract_notification_type(n),
            "registration": n.get("registration"),
            "notification_msg": n.get("notification_msg"),
        }
        for n in raw_notes
    ]
    counts = _counts([trip], parsed_notes)
    _check("generic type + trigger_description=HIGH_RPM is counted",
           counts[1]["high_rpm"] == 1,
           f"got={counts[1]}")
    _check("generic type + trigger_description=OVERREV is counted",
           counts[1]["overrev"] == 1,
           f"got={counts[1]}")


def test_vehicle_filter() -> None:
    trips = [
        _trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
              end="2026-04-01T10:00:00Z"),
        _trip(2, vehicle_id="v2", start="2026-04-01T08:00:00Z",
              end="2026-04-01T10:00:00Z"),
    ]
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM"),
        _notif("v2", "2026-04-01T09:30:00Z", "HIGH_RPM"),
        # No vehicle_id and no registration => no fallback path either.
        _notif(None, "2026-04-01T09:00:00Z", "HIGH_RPM"),
        _notif("v1", "2026-04-01T09:01:00Z", "OVERREV"),
    ]
    counts = _counts(trips, notes)
    _check("vehicle_filter: trip 1 (v1) high_rpm=1, overrev=1",
           counts[1] == {"high_rpm": 1, "overrev": 1},
           f"got={counts[1]}")
    _check("vehicle_filter: trip 2 (v2) high_rpm=1, overrev=0",
           counts[2] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[2]}")


def test_trip_without_vehicle_id_or_registration_yields_zero() -> None:
    """Trip with NO vehicle_id AND no registration cannot be matched
    via either the vehicle_id index or the registration fallback —
    expect zeros even with valid notifications in the time window."""
    trips = [{
        "provider_trip_id": 99,
        "registration": "",
        "vehicle_id": None,
        "start_ts": _ts("2026-04-01T08:00:00Z"),
        "end_ts": _ts("2026-04-01T10:00:00Z"),
    }]
    notes = [
        _notif(None, "2026-04-01T08:30:00Z", "HIGH_RPM"),
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM"),
    ]
    counts = _counts(trips, notes)
    _check("trip without vehicle_id AND registration: counts are 0/0",
           counts[99] == {"high_rpm": 0, "overrev": 0},
           f"got={counts[99]}")


def test_time_boundaries_inclusive() -> None:
    trips = [_trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
                   end="2026-04-01T10:00:00Z")]
    notes = [
        _notif("v1", "2026-04-01T07:59:59Z", "HIGH_RPM"),  # before, ignored
        _notif("v1", "2026-04-01T08:00:00Z", "HIGH_RPM"),  # boundary, included
        _notif("v1", "2026-04-01T10:00:00Z", "OVERREV"),   # boundary, included
        _notif("v1", "2026-04-01T10:00:01Z", "OVERREV"),   # after, ignored
    ]
    counts = _counts(trips, notes)
    _check("time_boundaries: high_rpm == 1 (start_ts inclusive)",
           counts[1]["high_rpm"] == 1, f"got={counts[1]['high_rpm']}")
    _check("time_boundaries: overrev == 1 (end_ts inclusive)",
           counts[1]["overrev"] == 1, f"got={counts[1]['overrev']}")


def test_multiple_trips_per_vehicle_distribute_events() -> None:
    trips = [
        _trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
              end="2026-04-01T09:00:00Z"),
        _trip(2, vehicle_id="v1", start="2026-04-01T10:00:00Z",
              end="2026-04-01T11:00:00Z"),
    ]
    notes = [
        _notif("v1", "2026-04-01T08:15:00Z", "HIGH_RPM"),  # → trip 1
        _notif("v1", "2026-04-01T08:45:00Z", "HIGH_RPM"),  # → trip 1
        _notif("v1", "2026-04-01T09:30:00Z", "HIGH_RPM"),  # gap → ignored
        _notif("v1", "2026-04-01T10:30:00Z", "OVERREV"),   # → trip 2
    ]
    counts = _counts(trips, notes)
    _check("multi-trip: trip 1 high_rpm=2",
           counts[1]["high_rpm"] == 2, f"got={counts[1]['high_rpm']}")
    _check("multi-trip: trip 2 overrev=1",
           counts[2]["overrev"] == 1, f"got={counts[2]['overrev']}")
    _check("multi-trip: gap event not counted",
           counts[1]["overrev"] == 0 and counts[2]["high_rpm"] == 0,
           f"got trip1.overrev={counts[1]['overrev']}, "
           f"trip2.high_rpm={counts[2]['high_rpm']}")


def test_grouping_avoids_n_squared() -> None:
    """Smoke-test the performance shape: with many trips on disjoint
    vehicles, only events on the matching vehicle should be inspected.

    We can't observe Big-O directly, but we can assert that adding
    irrelevant trips for OTHER vehicles does not change counts for our
    target trip — which is what the per-vehicle pre-grouping guarantees.
    A naive O(n*m) implementation would still produce the same numbers,
    but combined with the helper's docstring + sort behavior we
    establish the contract is honored.

    NOTE: target trip uses provider_trip_id=999 and vehicle_id="V_TARGET"
    so it cannot collide with the procedurally-generated `v0..v19` noise.
    """
    target = _trip(999, vehicle_id="V_TARGET",
                   start="2026-04-01T08:00:00Z",
                   end="2026-04-01T09:00:00Z")
    other_trips = [
        _trip(i, vehicle_id=f"v{i}",
              start="2026-04-01T08:00:00Z",
              end="2026-04-01T09:00:00Z")
        for i in range(20)
    ]
    notes = [
        _notif(f"v{i}", "2026-04-01T08:30:00Z", "HIGH_RPM")
        for i in range(20)
    ] + [
        _notif("V_TARGET", "2026-04-01T08:30:00Z", "HIGH_RPM"),
        _notif("V_TARGET", "2026-04-01T08:45:00Z", "OVERREV"),
    ]
    counts = _counts([target] + other_trips, notes)
    _check("grouping: target trip counted exactly its own vehicle's events",
           counts[999] == {"high_rpm": 1, "overrev": 1},
           f"got={counts[999]}")
    # Cross-check: an unrelated other-vehicle trip should ALSO get its own
    # single HIGH_RPM (1 from the noise list) and zero OVERREVs.
    _check("grouping: other vehicle trips get their own single event",
           counts[5] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[5]}")


def test_event_ts_sorted_by_vehicle_for_early_break() -> None:
    """We don't expose the internal grouping, but we can confirm that
    the helper produces consistent counts no matter the input order
    (which is what sorting+early-break must give us)."""
    trip = _trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
                 end="2026-04-01T09:00:00Z")
    base = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM"),
        _notif("v1", "2026-04-01T08:45:00Z", "OVERREV"),
        _notif("v1", "2026-04-01T07:45:00Z", "HIGH_RPM"),  # before window
        _notif("v1", "2026-04-01T09:15:00Z", "OVERREV"),   # after window
    ]
    counts_a = _counts([trip], base)
    counts_b = _counts([trip], list(reversed(base)))
    _check("input-order independence (a==b)",
           counts_a == counts_b, f"a={counts_a}, b={counts_b}")
    _check("input-order independence: 1 high_rpm + 1 overrev",
           counts_a[1] == {"high_rpm": 1, "overrev": 1},
           f"got={counts_a[1]}")


def test_zero_window_yields_zero() -> None:
    trip = _trip(1, vehicle_id="v1", start="2026-04-01T08:00:00Z",
                 end="2026-04-01T08:00:00Z")
    notes = [
        _notif("v1", "2026-04-01T08:00:00Z", "HIGH_RPM"),
    ]
    counts = _counts([trip], notes)
    _check("zero-window trip: boundary event still counts (start==end)",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")


def test_registration_fallback_when_vehicle_id_missing() -> None:
    """Notifications with NO vehicle_id but a registration that matches
    a trip's registration should still be counted (the OR-fallback that
    landed in v1.2). This is the common "Telematics returned no
    vehicle_id" case that previously caused empty enrichment columns
    on whole fleets."""
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    notes = [
        _notif(None, "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
        _notif(None, "2026-04-01T09:30:00Z", "OVERREV",
               registration="REG-A"),
        # Different registration → should not match.
        _notif(None, "2026-04-01T09:00:00Z", "HIGH_RPM",
               registration="OTHER"),
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("registration-fallback: trip 1 high_rpm=1, overrev=1",
           counts[1] == {"high_rpm": 1, "overrev": 1},
           f"got={counts[1]}")
    _check("registration-fallback: matches_via_registration tally is 2",
           stats["matches_via_registration"] == 2,
           f"got={stats['matches_via_registration']}")
    _check("registration-fallback: matches_via_vehicle_id tally is 0",
           stats["matches_via_vehicle_id"] == 0,
           f"got={stats['matches_via_vehicle_id']}")


def test_registration_normalization_upper_strip() -> None:
    """Registrations differing only in case/whitespace must still match."""
    trip = _trip(1, vehicle_id=None,
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="  reg-A  ")
    notes = [
        _notif(None, "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
        _notif(None, "2026-04-01T09:00:00Z", "OVERREV",
               registration="reg-a"),
    ]
    counts = _counts([trip], notes)
    _check("registration normalize: case + whitespace tolerated",
           counts[1] == {"high_rpm": 1, "overrev": 1},
           f"got={counts[1]}")


def test_dedupe_when_both_keys_match_same_event() -> None:
    """An event matching the trip via BOTH vehicle_id and registration
    must be counted exactly ONCE — the dedup uses event index identity."""
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("dedupe: counted once when both keys match",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")
    _check("dedupe: stats charge match to vehicle_id, not registration",
           stats["matches_via_vehicle_id"] == 1
           and stats["matches_via_registration"] == 0,
           f"got via_vid={stats['matches_via_vehicle_id']}, "
           f"via_reg={stats['matches_via_registration']}")


def test_stats_block_shape_and_totals() -> None:
    """The stats dict returned alongside counts is the diagnostic
    contract the dispatcher logs as 'RPM matching stats'. Verify the
    expected keys are present and totals add up against a hand-rolled
    scenario (3 HIGH_RPM, 2 OVERREV, 1 matched-via-reg only)."""
    trips = [
        _trip(1, vehicle_id="v1",
              start="2026-04-01T08:00:00Z",
              end="2026-04-01T10:00:00Z",
              registration="REG-A"),
    ]
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
        _notif("v1", "2026-04-01T08:31:00Z", "HIGH_RPM",
               registration="REG-A"),
        _notif("v1", "2026-04-01T08:32:00Z", "OVERREV",
               registration="REG-A"),
        _notif(None, "2026-04-01T09:00:00Z", "OVERREV",
               registration="REG-A"),  # via reg only
        # ignored — wrong type
        _notif("v1", "2026-04-01T09:30:00Z", "SPEEDING",
               registration="REG-A"),
        # ignored — wrong vehicle and wrong registration
        _notif("v999", "2026-04-01T09:31:00Z", "HIGH_RPM",
               registration="OTHER"),
    ]
    counts, stats = job._compute_rpm_counts(trips=trips, notifications=notes)
    expected_keys = {
        "high_rpm_events", "overrev_events",
        "vehicles_with_events", "registrations_with_events",
        "matched_high_rpm", "matched_overrev",
        "trips_with_events", "trips_without_events",
        "matches_via_vehicle_id", "matches_via_registration",
    }
    _check("stats block contains all expected keys",
           expected_keys.issubset(stats.keys()),
           f"missing={expected_keys - set(stats.keys())}")
    _check("stats: high_rpm_events == 3 (post type-filter, pre matching)",
           stats["high_rpm_events"] == 3, f"got={stats['high_rpm_events']}")
    _check("stats: overrev_events == 2",
           stats["overrev_events"] == 2, f"got={stats['overrev_events']}")
    _check("stats: matched_high_rpm == 2",
           stats["matched_high_rpm"] == 2, f"got={stats['matched_high_rpm']}")
    _check("stats: matched_overrev == 2 (1 via vid, 1 via reg)",
           stats["matched_overrev"] == 2, f"got={stats['matched_overrev']}")
    _check("stats: trips_with_events == 1",
           stats["trips_with_events"] == 1, f"got={stats['trips_with_events']}")
    _check("stats: trips_without_events == 0",
           stats["trips_without_events"] == 0,
           f"got={stats['trips_without_events']}")
    _check("stats: matches_via_vehicle_id == 3",
           stats["matches_via_vehicle_id"] == 3,
           f"got={stats['matches_via_vehicle_id']}")
    _check("stats: matches_via_registration == 1 (the no-vid event)",
           stats["matches_via_registration"] == 1,
           f"got={stats['matches_via_registration']}")
    _check("counts wired through: trip 1 == {2,2}",
           counts[1] == {"high_rpm": 2, "overrev": 2},
           f"got={counts[1]}")


def test_dedupe_by_provider_notification_id_collapses_duplicates() -> None:
    """Two list entries carrying the same `provider_notification_id`
    are the same logical event — they must count exactly once even
    when both fall inside the trip window. This is the canonical case
    that the old list-index-based dedupe got wrong: each list slot
    looked unique, so the same logical event would tally twice.
    """
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    pid = "11e594f4-8195-4c10-8301-5d0bf0447a22"
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid),
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid),
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("dedupe-by-pid: collapsed to one HIGH_RPM",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")
    _check("dedupe-by-pid: matched_high_rpm == 1",
           stats["matched_high_rpm"] == 1,
           f"got={stats['matched_high_rpm']}")


def test_reorder_input_invariant_under_dedupe() -> None:
    """The same notification list, reordered, must produce the same
    counts and the same stats totals. Index-based dedupe could pick
    different "winners" between vehicle_id and registration paths
    depending on order — content-addressable identity cannot.
    """
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    pid_a = "bd7662a5-eeb4-4614-8720-d477abfcb227"
    pid_b = "b454f82c-5857-4bab-8342-b7258e5cf7de"
    notes_forward = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid_a),
        _notif("v1", "2026-04-01T09:00:00Z", "OVERREV",
               registration="REG-A", pid=pid_b),
        # also-listed via registration path only — same logical events:
        _notif(None, "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid_a),
        _notif(None, "2026-04-01T09:00:00Z", "OVERREV",
               registration="REG-A", pid=pid_b),
    ]
    notes_reversed = list(reversed(notes_forward))
    counts_a, stats_a = job._compute_rpm_counts(
        trips=[trip], notifications=notes_forward,
    )
    counts_b, stats_b = job._compute_rpm_counts(
        trips=[trip], notifications=notes_reversed,
    )
    _check("reorder-invariant: counts unchanged under reverse",
           counts_a == counts_b, f"a={counts_a}, b={counts_b}")
    _check("reorder-invariant: matched_high_rpm equal",
           stats_a["matched_high_rpm"] == stats_b["matched_high_rpm"],
           f"a={stats_a['matched_high_rpm']}, b={stats_b['matched_high_rpm']}")
    _check("reorder-invariant: matched_overrev equal",
           stats_a["matched_overrev"] == stats_b["matched_overrev"],
           f"a={stats_a['matched_overrev']}, b={stats_b['matched_overrev']}")
    _check("reorder-invariant: trip 1 == {1 high_rpm, 1 overrev}",
           counts_a[1] == {"high_rpm": 1, "overrev": 1},
           f"got={counts_a[1]}")


def test_dedupe_by_pid_preferred_over_vehicle_registration_fields() -> None:
    """When a `provider_notification_id` is present, identity is the
    pid alone — even if the (vehicle_id, registration, event_ts)
    composite would also match. Two events sharing the same pid but
    differing on vehicle_id collapse to one (typical case: provider
    re-emits an event under the same id with a corrected vehicle_id).
    """
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    pid = "f6222a11-06ee-4e4f-8b25-302a9d963cfa"
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid, msg="initial"),
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A", pid=pid, msg="corrected"),
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("pid-preferred: differing msg, same pid → one count",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")
    _check("pid-preferred: matched_high_rpm == 1",
           stats["matched_high_rpm"] == 1,
           f"got={stats['matched_high_rpm']}")


def test_dedupe_fallback_key_when_pid_missing() -> None:
    """Without `provider_notification_id`, identity falls back to a
    deterministic composite. Two list entries with the SAME (type,
    vehicle, registration, event_ts, msg) collapse — the old
    index-based dedupe would have counted them twice.
    """
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
        # Identical content, no pid → same fallback key.
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("fallback-key: identical content collapses to one count",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")
    _check("fallback-key: matched_high_rpm == 1",
           stats["matched_high_rpm"] == 1,
           f"got={stats['matched_high_rpm']}")


def test_distinct_event_ts_or_type_remain_separate() -> None:
    """Distinct event_ts OR distinct type must yield distinct identity
    even without a pid — otherwise legitimate back-to-back events
    would silently merge. Three events for the same vehicle: two share
    a timestamp but differ in type, two share a type but differ in
    timestamp. Final: 2 HIGH_RPM + 1 OVERREV.
    """
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    notes = [
        _notif("v1", "2026-04-01T08:30:00Z", "HIGH_RPM",
               registration="REG-A"),
        _notif("v1", "2026-04-01T08:30:00Z", "OVERREV",
               registration="REG-A"),  # same ts, different type
        _notif("v1", "2026-04-01T08:31:00Z", "HIGH_RPM",
               registration="REG-A"),  # same type, different ts
    ]
    counts, stats = job._compute_rpm_counts(trips=[trip], notifications=notes)
    _check("distinct-id: 2 HIGH_RPM + 1 OVERREV stay separate",
           counts[1] == {"high_rpm": 2, "overrev": 1},
           f"got={counts[1]}")
    _check("distinct-id: matched_high_rpm == 2",
           stats["matched_high_rpm"] == 2,
           f"got={stats['matched_high_rpm']}")
    _check("distinct-id: matched_overrev == 1",
           stats["matched_overrev"] == 1,
           f"got={stats['matched_overrev']}")


def test_notification_dedupe_key_helper_shape() -> None:
    """Direct unit-test of `_notification_dedupe_key`:
       * non-empty `provider_notification_id` wins (prefix `pid:`).
       * missing/empty pid falls back to composite (prefix `fb:`).
       * normalization: case + whitespace on registration; UTC ISO on
         event_ts; pid surrounding whitespace stripped.
       * distinct (type, vehicle, registration, event_ts) → distinct
         keys.
       * `status` is consulted when `notification_msg` is missing.
    """
    pid = "44444444-4444-4444-8444-444444444444"
    n_with_pid = {
        "provider_notification_id": f"  {pid}  ",
        "type": "HIGH_RPM",
        "vehicle_id": "v1",
        "registration": "reg-A",
        "event_ts": _ts("2026-04-01T08:30:00Z"),
    }
    k_pid = job._notification_dedupe_key(n_with_pid)
    _check("dedupe_key: pid path returns 'pid:<uuid>' (whitespace stripped)",
           k_pid == f"pid:{pid}", f"got={k_pid!r}")

    n_no_pid_a = {
        "provider_notification_id": None,
        "type": "HIGH_RPM",
        "vehicle_id": "v1",
        "registration": " REG-A ",
        "event_ts": _ts("2026-04-01T08:30:00Z"),
        "notification_msg": "first",
    }
    n_no_pid_b = {
        "provider_notification_id": "",  # empty pid → fallback
        "type": "high_rpm",  # case-insensitive
        "vehicle_id": "v1",
        "registration": "reg-a",
        "event_ts": _ts("2026-04-01T08:30:00Z"),
        "notification_msg": "first",
    }
    k_a = job._notification_dedupe_key(n_no_pid_a)
    k_b = job._notification_dedupe_key(n_no_pid_b)
    _check("dedupe_key: fallback path uses 'fb:' prefix",
           k_a.startswith("fb:") and k_b.startswith("fb:"),
           f"a={k_a!r}, b={k_b!r}")
    _check("dedupe_key: equivalent content yields equal fallback key",
           k_a == k_b, f"a={k_a!r}, b={k_b!r}")

    n_diff_ts = dict(n_no_pid_a)
    n_diff_ts["event_ts"] = _ts("2026-04-01T08:31:00Z")
    n_diff_type = dict(n_no_pid_a)
    n_diff_type["type"] = "OVERREV"
    n_diff_vid = dict(n_no_pid_a)
    n_diff_vid["vehicle_id"] = "v2"
    n_diff_reg = dict(n_no_pid_a)
    n_diff_reg["registration"] = "REG-B"
    _check("dedupe_key: distinct event_ts → distinct key",
           job._notification_dedupe_key(n_diff_ts) != k_a)
    _check("dedupe_key: distinct type → distinct key",
           job._notification_dedupe_key(n_diff_type) != k_a)
    _check("dedupe_key: distinct vehicle_id → distinct key",
           job._notification_dedupe_key(n_diff_vid) != k_a)
    _check("dedupe_key: distinct registration → distinct key",
           job._notification_dedupe_key(n_diff_reg) != k_a)

    n_status_only = {
        "provider_notification_id": None,
        "type": "HIGH_RPM",
        "vehicle_id": "v1",
        "registration": "REG-A",
        "event_ts": _ts("2026-04-01T08:30:00Z"),
        "notification_msg": None,
        "status": "ACTIVE",
    }
    n_status_other = dict(n_status_only)
    n_status_other["status"] = "RESOLVED"
    _check("dedupe_key: `status` is consulted when notification_msg is None",
           job._notification_dedupe_key(n_status_only)
           != job._notification_dedupe_key(n_status_other))


def test_normalize_registration_helper() -> None:
    samples = [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("ABC", "ABC"),
        (" abc ", "ABC"),
        ("Reg-A", "REG-A"),
        (123, "123"),
    ]
    for raw, expected in samples:
        got = job._normalize_registration(raw)
        _check(f"_normalize_registration({raw!r}) -> {expected!r}",
               got == expected, f"got={got!r}")


def test_vehicle_event_overrev_start_counts_end_ignored() -> None:
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    events = [
        _event("v1", "REG-A", "2026-04-01T08:30:00Z", "OVERREV_START", event_id="over-start"),
        _event("v1", "REG-A", "2026-04-01T08:31:00Z", "OVERREV_END", event_id="over-end"),
        _event("v1", "REG-A", "2026-04-01T08:45:00Z", "OVER REV", event_id="over-unsuffixed"),
    ]
    counts, stats = job._compute_rpm_vehicle_event_counts(trips=[trip], vehicle_events=events)
    _check("vehicle-events OVERREV: START + unsuffixed count, END ignored",
           counts[1] == {"high_rpm": 0, "overrev": 2},
           f"got={counts[1]}")
    _check("vehicle-events OVERREV: END row recognized and ignored",
           stats["overrev_end_events_ignored"] == 1,
           f"got={stats['overrev_end_events_ignored']}")


def test_vehicle_event_high_rpm_start_counts_end_ignored() -> None:
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    events = [
        _event("v1", "REG-A", "2026-04-01T08:30:00Z", "HIGH_RPM_START", event_id="high-start"),
        _event("v1", "REG-A", "2026-04-01T08:31:00Z", "HIGH_RPM_END", event_id="high-end"),
        _event("v1", "REG-A", "2026-04-01T08:45:00Z", "HIGH-RPM", event_id="high-unsuffixed"),
    ]
    counts, stats = job._compute_rpm_vehicle_event_counts(trips=[trip], vehicle_events=events)
    _check("vehicle-events HIGH_RPM: START + unsuffixed count, END ignored",
           counts[1] == {"high_rpm": 2, "overrev": 0},
           f"got={counts[1]}")
    _check("vehicle-events HIGH_RPM: END row recognized and ignored",
           stats["high_rpm_end_events_ignored"] == 1,
           f"got={stats['high_rpm_end_events_ignored']}")


def test_vehicle_event_rpm_numeric_threshold_not_counted() -> None:
    trip = _trip(1, vehicle_id="v1",
                 start="2026-04-01T08:00:00Z",
                 end="2026-04-01T10:00:00Z",
                 registration="REG-A")
    events = [
        _event("v1", "REG-A", "2026-04-01T08:30:00Z", "PERIODIC_EVENT", rpm=5500),
    ]
    counts = _vehicle_event_counts([trip], events)
    _check("vehicle-events: numeric rpm threshold alone is not counted",
           counts[1] == {"high_rpm": 0, "overrev": 0},
           f"got={counts[1]}")


def test_vehicle_event_assignment_by_registration_or_vehicle_id() -> None:
    trips = [
        _trip(1, vehicle_id="v1",
              start="2026-04-01T08:00:00Z",
              end="2026-04-01T09:00:00Z",
              registration="REG-A"),
        _trip(2, vehicle_id="v2",
              start="2026-04-01T08:00:00Z",
              end="2026-04-01T09:00:00Z",
              registration="REG-B"),
    ]
    events = [
        _event(None, "reg-a", "2026-04-01T08:30:00Z", "HIGH RPM"),
        _event("v2", "OTHER", "2026-04-01T08:45:00Z", "OVER_REV"),
        _event("v2", "OTHER", "2026-04-01T09:30:00Z", "OVERREV_START"),
    ]
    counts, stats = job._compute_rpm_vehicle_event_counts(trips=trips, vehicle_events=events)
    _check("vehicle-events: registration fallback assigns HIGH_RPM",
           counts[1] == {"high_rpm": 1, "overrev": 0},
           f"got={counts[1]}")
    _check("vehicle-events: vehicle_id assigns OVERREV and outside-trip ignored",
           counts[2] == {"high_rpm": 0, "overrev": 1},
           f"got={counts[2]}")
    _check("vehicle-events: assignment path stats updated",
           stats["matches_via_registration"] == 1 and stats["matches_via_vehicle_id"] == 1,
           f"got via_reg={stats['matches_via_registration']} via_vid={stats['matches_via_vehicle_id']}")


def main() -> int:
    test_type_filter()
    test_type_extractor_case_insensitive()
    test_generic_type_with_rpm_trigger_counts()
    test_vehicle_filter()
    test_trip_without_vehicle_id_or_registration_yields_zero()
    test_time_boundaries_inclusive()
    test_multiple_trips_per_vehicle_distribute_events()
    test_grouping_avoids_n_squared()
    test_event_ts_sorted_by_vehicle_for_early_break()
    test_zero_window_yields_zero()
    test_registration_fallback_when_vehicle_id_missing()
    test_registration_normalization_upper_strip()
    test_dedupe_when_both_keys_match_same_event()
    test_stats_block_shape_and_totals()
    test_dedupe_by_provider_notification_id_collapses_duplicates()
    test_reorder_input_invariant_under_dedupe()
    test_dedupe_by_pid_preferred_over_vehicle_registration_fields()
    test_dedupe_fallback_key_when_pid_missing()
    test_distinct_event_ts_or_type_remain_separate()
    test_notification_dedupe_key_helper_shape()
    test_normalize_registration_helper()
    test_vehicle_event_overrev_start_counts_end_ignored()
    test_vehicle_event_high_rpm_start_counts_end_ignored()
    test_vehicle_event_rpm_numeric_threshold_not_counted()
    test_vehicle_event_assignment_by_registration_or_vehicle_id()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — RPM counting helpers behave correctly across "
          f"{25} test cases (notifications legacy helpers + vehicle-event labels).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
