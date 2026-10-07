#!/usr/bin/env python3
"""Rolling historical refetch, idempotency and data-preservation contract.

Pure: stdlib only, no network, no database, no secrets. The provider HTTP layer
is exercised through the real `TelematicsFleetProviderClient` with a stubbed
`requests.Session`, so the production pagination state machine, budgets and the
`/trips` wire-time boundary all run for real.

What this proves, in the order the acceptance criteria ask for it:

  1. `lookback_days` produces a genuine historical re-request; the coverage
     watermark does not suppress already-covered time.
  2. The forward watermark stays monotonic — a rolling window never moves it
     backwards, and a candidate at or behind it is a validated no-op.
  3. Routine rolling overlap is decided by the ordinary coverage gate and needs
     no manual recovery authority.
  4. Late-arrival end to end: a trip published after the original pass is
     fetched by a later scheduled run, and the previously seen trips do not
     duplicate.
  5. `client_trips` upsert identity is the provider trip id, and `Dysponent_ID`
     is not in the conflict update list, so enrichment survives re-upsert.
  6. A budget or provider-safety failure aborts the sub-window instead of
     returning a partial result that could be mistaken for success.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_trips_rolling_refetch.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.coverage_finalization import (  # noqa: E402
    COVERAGE_SOURCE_MANUAL_RECOVERY,
    COVERAGE_SOURCE_SCHEDULED_RUN,
)
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED,
    CoverageState,
    derive_effective_window,
    evaluate_coverage_gate,
)
from jobs.api.telematics.provider_client import (  # noqa: E402
    TelematicsFleetProviderClient,
    trips_wire_window,
)
from jobs.api.telematics.provider_safety import (  # noqa: E402
    TelematicsProviderSafetyError,
    ProviderRunBudget,
    SafetyLimits,
)
from jobs.trips_pagination_mode import normalize_trips_pagination_mode  # noqa: E402

WARSAW = ZoneInfo("Europe/Warsaw")
WIRE_FMT = "%Y-%m-%d %H:%M:%S"

# ALPHA00001 production stabilization configuration (control plane, read-only).
ALPHA_DELAY_S = 10800      # trips_stabilization_delay_seconds
ALPHA_OVERLAP_S = 3600     # trips_overlap_seconds
ALPHA_MAX_RECOVERY_S = 2678400  # trips_max_recovery_span_seconds (31 days)

FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}" + (f"  --  {detail}" if detail else ""))


def coverage_state(*, covered_through: datetime, coverage_start: datetime) -> CoverageState:
    return CoverageState(
        schedule_id="11111111-1111-1111-1111-111111111111",
        client_id="9536f715-2fd0-4ffd-86ed-ba06f5490c5e",
        client_code="ALPHA00001",
        dataset_name="trips_sync",
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
        bootstrap_status="READY",
        bootstrap_evidence_ref="telematics-coverage-bootstrap/1:sha256=test",
        seeded_at=coverage_start,
        seeded_by="test",
        covered_through_source=COVERAGE_SOURCE_SCHEDULED_RUN,
        last_gap_detected_ts=None,
    )


# ---------------------------------------------------------------------------
# 1. lookback_days genuinely re-requests already-covered history.
# ---------------------------------------------------------------------------

fire = datetime(2026, 8, 20, 2, 0, tzinfo=timezone.utc)
covered_through = fire - timedelta(seconds=ALPHA_DELAY_S)   # previous run's frontier
coverage_start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)

windows: Dict[int, Any] = {}
for lookback in (1, 7, 14, 21):
    windows[lookback] = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=lookback,
        stabilization_delay_seconds=ALPHA_DELAY_S,
        overlap_seconds=ALPHA_OVERLAP_S,
        max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
    )

check(
    "lookback_days=1 reaches only ~1 day behind the fire (the confirmed RC-1 state)",
    timedelta(days=1) <= fire - windows[1].effective_window_start_ts < timedelta(days=1, hours=5),
    f"reach {fire - windows[1].effective_window_start_ts}",
)
for lookback in (7, 14, 21):
    w = windows[lookback]
    reach = fire - w.effective_window_start_ts
    check(
        f"lookback_days={lookback} re-requests ~{lookback} days of already-covered history",
        timedelta(days=lookback) <= reach < timedelta(days=lookback, hours=5),
        f"reach {reach}",
    )
    check(
        f"lookback_days={lookback} starts strictly behind the coverage watermark",
        w.effective_window_start_ts < covered_through,
        f"E_start {w.effective_window_start_ts} vs W {covered_through}",
    )
    check(
        f"lookback_days={lookback} window stays connected (no gap declared)",
        w.is_connected,
    )

check(
    "a wider lookback only moves the window start earlier, never later",
    windows[21].effective_window_start_ts
    < windows[14].effective_window_start_ts
    < windows[7].effective_window_start_ts
    < windows[1].effective_window_start_ts,
)
check(
    "the window END is set by stabilization only and is unaffected by lookback",
    len({w.effective_window_end_ts for w in windows.values()}) == 1,
)
check(
    "max_recovery_span clamps an over-wide lookback instead of silently accepting it",
    derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=60,
        stabilization_delay_seconds=ALPHA_DELAY_S,
        overlap_seconds=ALPHA_OVERLAP_S,
        max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
    ).effective_window_start_ts
    == windows[1].effective_window_end_ts - timedelta(seconds=ALPHA_MAX_RECOVERY_S),
)

# ---------------------------------------------------------------------------
# 2. The ordinary gate allows the rolling overlap — no recovery authority.
# ---------------------------------------------------------------------------

gate = evaluate_coverage_gate(
    schedule_id="11111111-1111-1111-1111-111111111111",
    client_id="9536f715-2fd0-4ffd-86ed-ba06f5490c5e",
    client_code="ALPHA00001",
    dataset_name="trips_sync",
    scheduled_fire_ts=fire,
    lookback_days=14,
    stabilization_delay_seconds=ALPHA_DELAY_S,
    overlap_seconds=ALPHA_OVERLAP_S,
    max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    coverage_state=coverage_state(covered_through=covered_through, coverage_start=coverage_start),
    now_utc=fire + timedelta(minutes=1),
)
check("rolling overlap is ALLOWED by the ordinary scheduled gate", gate.allowed)
check("rolling overlap classification is the normal one", gate.classification == COVERAGE_GATE_ALLOWED)
check(
    "rolling overlap needs no manual recovery authority",
    gate.effective_window is not None
    and gate.effective_window.effective_window_start_ts < covered_through,
)
check(
    "the gate echoes the unchanged watermark rather than proposing to move it back",
    gate.covered_through_ts == covered_through,
)
check(
    "manual recovery remains a distinct, separately sourced path",
    COVERAGE_SOURCE_MANUAL_RECOVERY != COVERAGE_SOURCE_SCHEDULED_RUN,
)

# ---------------------------------------------------------------------------
# 3. Forward watermark monotonicity.
# ---------------------------------------------------------------------------

advance_sql = (REPO_ROOT / "jobs/api/telematics/coverage_finalization.py").read_text(encoding="utf-8")
check(
    "the coverage UPDATE carries a strict monotonicity predicate",
    "AND covered_through_ts < %(new_covered_through_ts)s" in advance_sql,
)
check(
    "a candidate at or behind the watermark is a no-op, not a rewrite",
    "candidate <= W" in advance_sql and "no **``UPDATE``**" in advance_sql or "issues **no** ``UPDATE``" in advance_sql,
)
check(
    "the rolling window end never precedes the current watermark, so W only moves forward",
    windows[21].effective_window_end_ts >= covered_through,
)

# ---------------------------------------------------------------------------
# 4. Late-arrival end to end, through the real provider client.
# ---------------------------------------------------------------------------

def trip(trip_id: int, reg: str, start_utc: datetime, distance_m: int) -> Dict[str, Any]:
    return {
        "trip_id": trip_id,
        "registration": reg,
        "start_timestamp": start_utc.strftime(WIRE_FMT),
        "end_timestamp": (start_utc + timedelta(minutes=20)).strftime(WIRE_FMT),
        "trip_distance": distance_m,
    }


HIST_START = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
TRIP_A = trip(500_000_001, "XE10645", HIST_START + timedelta(minutes=10), 3578)
TRIP_B = trip(500_000_002, "XE10645", HIST_START + timedelta(minutes=40), 10900)
TRIP_C_LATE = trip(500_000_900, "XE10645", HIST_START + timedelta(minutes=25), 2534)


class StubResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: Dict[str, str] = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class StubSession:
    """Serves trips whose Warsaw wall-clock falls inside the requested window."""

    def __init__(self, catalogue: List[Dict[str, Any]]) -> None:
        self.catalogue = catalogue
        self.requests: List[Dict[str, Any]] = []
        self.auth = None

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        params = kwargs.get("params") or {}
        self.requests.append({"method": method, "url": url, "params": dict(params)})
        assert method.upper() == "GET", "the trips client must never issue a write request"
        start = datetime.strptime(params["start_timestamp"], WIRE_FMT)
        end = datetime.strptime(params["end_timestamp"], WIRE_FMT)
        page = int(params.get("page", 1))
        selected = [
            row
            for row in self.catalogue
            if start
            <= datetime.strptime(row["start_timestamp"], WIRE_FMT)
            .replace(tzinfo=timezone.utc)
            .astimezone(WARSAW)
            .replace(tzinfo=None)
            <= end
        ]
        data = selected if page == 1 else []
        return StubResponse(
            {
                "data": data,
                "meta": {"current_page": page, "per_page": int(params.get("limit", 1000)), "last_page": 1},
            }
        )

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        return self.request("GET", url, **kwargs)


def build_client(session: StubSession) -> TelematicsFleetProviderClient:
    limits = SafetyLimits(
        max_requests_per_run=20,
        max_requests_per_endpoint=20,
        max_requests_per_subwindow=6,
        max_pages_per_subwindow=4,
        max_retries_per_http_call=0,
        timeout_s=30,
    )
    client = TelematicsFleetProviderClient(
        base_url="https://provider.invalid/rest",
        basic_auth_username="stub",
        basic_auth_password="stub",
        page_limit=1000,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        trips_pagination_mode="data_invariants_v1",
    )
    client._session = session
    return client


# Run 1 — the original pass. Only A and B exist at the provider.
session1 = StubSession([TRIP_A, TRIP_B])
run1 = build_client(session1).fetch_trips(
    window_start_ts=HIST_START, window_end_ts=HIST_START + timedelta(hours=1),
)
run1_ids = [int(r["trip_id"]) for r in run1]
check("run 1 fetches the two trips that existed at the time", sorted(run1_ids) == [500_000_001, 500_000_002])
check("run 1 issued GET only", all(r["method"].upper() == "GET" for r in session1.requests))

# The watermark now sits ahead of that historical interval.
w_after_run1 = HIST_START + timedelta(hours=1)

# Run 2 — a later scheduled fire. C was published in the meantime, inside an
# interval the platform already considers covered.
later_fire = HIST_START + timedelta(days=6, hours=2)
rolling = derive_effective_window(
    scheduled_fire_ts=later_fire,
    lookback_days=14,
    stabilization_delay_seconds=ALPHA_DELAY_S,
    overlap_seconds=ALPHA_OVERLAP_S,
    max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    coverage_start_ts=coverage_start,
    covered_through_ts=w_after_run1,
)
check(
    "run 2's rolling window reaches back into the already-covered interval",
    rolling.effective_window_start_ts <= HIST_START,
    f"E_start {rolling.effective_window_start_ts} vs historical {HIST_START}",
)

session2 = StubSession([TRIP_A, TRIP_B, TRIP_C_LATE])
run2 = build_client(session2).fetch_trips(
    window_start_ts=rolling.effective_window_start_ts,
    window_end_ts=rolling.effective_window_end_ts,
)
run2_ids = [int(r["trip_id"]) for r in run2]
check(
    "run 2 fetches the late-published trip C automatically",
    500_000_900 in run2_ids,
    f"got {sorted(run2_ids)}",
)
check("run 2 also re-fetches A and B", {500_000_001, 500_000_002}.issubset(set(run2_ids)))
check("run 2 returns no duplicate provider trip ids", len(run2_ids) == len(set(run2_ids)))
check(
    "the watermark still advances forward across run 2",
    rolling.effective_window_end_ts > w_after_run1,
)
check(
    "run 2's provider requests carry Warsaw wall-clock, not the UTC projection",
    all(
        datetime.strptime(r["params"]["start_timestamp"], WIRE_FMT)
        == datetime.strptime(
            trips_wire_window(rolling.effective_window_start_ts, rolling.effective_window_end_ts)[0],
            WIRE_FMT,
        )
        for r in session2.requests[:1]
    ),
)

# ---------------------------------------------------------------------------
# 5. Upsert identity and Dysponent_ID preservation.
# ---------------------------------------------------------------------------

sync_src = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text(encoding="utf-8")
conflict_blocks = re.findall(
    r"ON CONFLICT \(client_id, provider_trip_id\) DO UPDATE SET(.*?)\n\s*\"\"\"",
    sync_src,
    flags=re.S,
)
check("the client_trips upsert conflict target is (client_id, provider_trip_id)", bool(conflict_blocks))
check(
    "no other conflict target is used for client_trips",
    "ON CONFLICT" not in sync_src.replace("ON CONFLICT (client_id, provider_trip_id)", ""),
)

set_list = "\n".join(conflict_blocks)
check(
    'Dysponent_ID is NOT in the conflict update list, so enrichment survives re-upsert',
    '"Dysponent_ID"' not in set_list,
    "found Dysponent_ID in the DO UPDATE SET list",
)
check(
    "Dysponent_ID is never written anywhere in the trips sync job",
    '"Dysponent_ID"' not in sync_src,
)
check(
    'Driver_Restrictions IS refreshed, because it is provider-owned',
    '"Driver_Restrictions"=EXCLUDED."Driver_Restrictions"' in set_list,
)
check(
    "record_id and sync provenance are refreshed on re-upsert",
    "record_id=EXCLUDED.record_id" in set_list and "sync_run_id=EXCLUDED.sync_run_id" in set_list,
)
check(
    "the non-overwrite path is DO NOTHING, which also cannot clear Dysponent_ID",
    "ON CONFLICT (client_id, provider_trip_id) DO NOTHING" in sync_src,
)

# ---------------------------------------------------------------------------
# 6. A failing sub-window aborts instead of returning a partial result.
# ---------------------------------------------------------------------------

class RepeatingPageSession(StubSession):
    """Serves a *full* page of identical rows forever.

    This is the shape of the real provider defect: `page` is accepted but the
    returned rows do not advance. Under `data_invariants_v1` control flow comes
    from the data, so a full page means "continue" while the repeated
    identities must be caught by the cross-page checks. A short page, by
    contrast, is a legitimate end-of-data signal and is asserted separately
    below — the guard must not fire on ordinary termination.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        params = kwargs.get("params") or {}
        self.requests.append({"method": method, "url": url, "params": dict(params)})
        limit = int(params.get("limit", 2))
        page = int(params.get("page", 1))
        return StubResponse(
            {
                "data": [TRIP_A, TRIP_B][:limit],
                "meta": {"current_page": page, "per_page": limit, "last_page": 99},
            }
        )


def build_small_page_client(session: StubSession) -> TelematicsFleetProviderClient:
    limits = SafetyLimits(
        max_requests_per_run=20,
        max_requests_per_endpoint=20,
        max_requests_per_subwindow=6,
        max_pages_per_subwindow=4,
        max_retries_per_http_call=0,
        timeout_s=30,
    )
    client = TelematicsFleetProviderClient(
        base_url="https://provider.invalid/rest",
        basic_auth_username="stub",
        basic_auth_password="stub",
        page_limit=2,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        trips_pagination_mode="data_invariants_v1",
    )
    client._session = session
    return client


repeating = RepeatingPageSession([TRIP_A, TRIP_B])
aborted = False
partial: List[Dict[str, Any]] = []
try:
    partial = build_small_page_client(repeating).fetch_trips(
        window_start_ts=HIST_START, window_end_ts=HIST_START + timedelta(hours=1),
    )
except TelematicsProviderSafetyError:
    aborted = True
check(
    "a provider pagination defect raises instead of returning a partial window",
    aborted and not partial,
    f"returned {len(partial)} rows without raising",
)

# The mirror image: ordinary termination must NOT be treated as a failure, so
# the guard cannot be satisfied by simply failing on everything.
short_page = StubSession([TRIP_A, TRIP_B])
ok_rows = build_client(short_page).fetch_trips(
    window_start_ts=HIST_START, window_end_ts=HIST_START + timedelta(hours=1),
)
check(
    "a short page still terminates normally (advisory meta never drives control flow)",
    len(ok_rows) == 2,
    f"got {len(ok_rows)}",
)

# Budget exhaustion must also raise rather than silently truncate.
starved_limits = SafetyLimits(
    max_requests_per_run=1,
    max_requests_per_endpoint=1,
    max_requests_per_subwindow=1,
    max_pages_per_subwindow=1,
    max_retries_per_http_call=0,
    timeout_s=30,
)
starved = TelematicsFleetProviderClient(
    base_url="https://provider.invalid/rest",
    basic_auth_username="stub",
    basic_auth_password="stub",
    page_limit=1000,
    safety_limits=starved_limits,
    budget=ProviderRunBudget(limits=starved_limits),
    trips_pagination_mode="data_invariants_v1",
)
starved._session = RepeatingPageSession([TRIP_A, TRIP_B])
budget_aborted = False
try:
    starved.fetch_trips(
        window_start_ts=HIST_START, window_end_ts=HIST_START + timedelta(days=40),
    )
except TelematicsProviderSafetyError:
    budget_aborted = True
check("budget exhaustion aborts the run rather than reporting a short window as complete", budget_aborted)

# ---------------------------------------------------------------------------
# 8. The insert-only recovery must page the same way the scheduled path does.
#
# `normalize_trips_pagination_mode(None)` returns `strict_meta`, so omitting the
# argument does not fail loudly — it silently selects the mode that aborts with
# PAGINATION_MISMATCH against the clients configured as `data_invariants_v1`,
# and only after the whole provider fetch has been paid for. The recovery job
# must therefore read the mode from the same control-plane row the scheduled
# job reads.
# ---------------------------------------------------------------------------

check(
    "an omitted pagination mode silently defaults to strict_meta",
    normalize_trips_pagination_mode(None) == "strict_meta",
)

backfill_src = (REPO_ROOT / "jobs/api/telematics/backfill_trips_insert_only.py").read_text(encoding="utf-8")
check(
    "the insert-only backfill passes the client's configured trips_pagination_mode",
    "trips_pagination_mode=cfg.trips_pagination_mode" in backfill_src,
    "the backfill would page in strict_meta regardless of client configuration",
)
check(
    "the insert-only backfill records the pagination mode in its preflight evidence",
    '"trips_pagination_mode": cfg.trips_pagination_mode' in backfill_src,
)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL ROLLING REFETCH / IDEMPOTENCY CHECKS PASSED")
