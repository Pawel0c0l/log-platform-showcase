from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from api.timezone_utils import format_business_timestamp, get_business_timezone_name, set_pg_session_timezone
from jobs.api.telematics import client_trips_admission
from jobs.api.telematics import manual_recovery_authority
from jobs.api.telematics import record_id as record_id_mod
from jobs.api.telematics.control_plane import (
    ClientAccountConfig,
    _platform_pg_conn,
    load_client_account_config,
    load_dataset_schedule,
    load_manual_recovery_claim,
)
from jobs.api.telematics.execution_outcome import (
    SKIP_REASON_DISABLED_SCHEDULE,
    ExecutionOutcomeRecorder,
)
from jobs.api.telematics.provider_client import (
    VEHICLE_EVENTS_MAX_INTENDED_WINDOW,
    TelematicsFleetProviderClient,
    provider_page_limit_from_env,
    sub_window_label as provider_sub_window_label,
)
from jobs.api.telematics.provider_safety import TelematicsProviderSafetyError, ProviderRunBudget, SafetyLimits
from jobs.api.telematics.request_evidence import (
    RequestEvidenceCollector,
    persist_pending_request_facts,
)
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
    is_required_trip_metrics_source,
    trip_metrics_source_skip_context,
)
from jobs.trips_pagination_mode import (
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_PAGINATION_MODE_DEFAULT,
    normalize_trips_pagination_mode,
)


JOB_SOURCE = "jobs.api.telematics.sync_trips_and_speeding"
DATASET_NAME = "trips_sync"
VEHICLE_EVENTS_FLEET_LIMIT_ENV = "TELEMATICS_PROVIDER_VEHICLE_EVENTS_LIMIT"
VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV = "TELEMATICS_PROVIDER_VEHICLE_EVENTS_MAX_PAGES_PER_DAY"
VEHICLE_EVENTS_FLEET_DEFAULT_LIMIT = 1000
VEHICLE_EVENTS_FLEET_MAX_LIMIT = 1000
VEHICLE_EVENTS_FLEET_DEFAULT_MAX_PAGES = 500
VEHICLE_EVENTS_CHUNK_HOURS_ENV = "TELEMATICS_EVENTS_CHUNK_HOURS"
VEHICLE_EVENTS_MIN_CHUNK_MINUTES_ENV = "TELEMATICS_EVENTS_MIN_CHUNK_MINUTES"
VEHICLE_EVENTS_CANDIDATE_COALESCE_MINUTES_ENV = (
    "TELEMATICS_EVENTS_CANDIDATE_COALESCE_MINUTES"
)
VEHICLE_EVENTS_CANDIDATE_MAX_WINDOWS_ENV = "TELEMATICS_EVENTS_CANDIDATE_MAX_WINDOWS"
VEHICLE_EVENTS_TIMEOUT_S_ENV = "TELEMATICS_EVENTS_TIMEOUT_S"
VEHICLE_EVENTS_RATE_LIMIT_RPS_ENV = "TELEMATICS_EVENTS_RATE_LIMIT_RPS"
VEHICLE_EVENTS_ENABLE_REGISTRATION_FALLBACK_ENV = "TELEMATICS_EVENTS_ENABLE_REGISTRATION_FALLBACK"
VEHICLE_EVENTS_REGISTRATION_FALLBACK_RPS_ENV = "TELEMATICS_EVENTS_REGISTRATION_FALLBACK_RPS"
VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS_ENV = (
    "TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS"
)
VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS_ENV = "TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS"
VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN_ENV = (
    "TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN"
)
VEHICLE_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES_ENV = (
    "TELEMATICS_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES"
)
VEHICLE_EVENTS_ENRICHMENT_MODE_ENV = "TELEMATICS_EVENTS_ENRICHMENT_MODE"
VEHICLE_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES_ENV = (
    "TELEMATICS_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES"
)
VEHICLE_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES_ENV = (
    "TELEMATICS_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES"
)
VEHICLE_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN_ENV = "TELEMATICS_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN"
VEHICLE_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH_ENV = "TELEMATICS_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH"
VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS = 4
VEHICLE_EVENTS_DEFAULT_MIN_CHUNK_MINUTES = 30
VEHICLE_EVENTS_DEFAULT_RATE_LIMIT_RPS = 2.5
VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_RPS = 1.0
VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MAX_REGISTRATIONS = 1500
VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MAX_CHUNKS = 1
VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES = 5
VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED = "enabled"
VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED = "disabled"
VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT = "strict"
VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT = "audited_best_effort"

#: How much of the trip window this run is allowed to buy events for.
#:
#: `window` is the historical contract: fetch every fleet event in the whole
#: trip window and recompute metrics for every trip in it. That is correct, and
#: for a base run it is also what you want — a base run re-reads a short window
#: precisely so a late event can still correct yesterday's count.
#:
#: `reconciliation_candidates` exists because a reconciliation window is long
#: for a reason that has nothing to do with events. It reaches back 16 days to
#: catch a *trip* the provider delivered late; almost every trip it re-reads was
#: already captured and already enriched days ago. Buying 16 days of fleet
#: events to re-derive metrics that are already correct is what tied the width
#: of the trip window to the event spend and put FOXTROT at 86% of a hard endpoint
#: budget. The window this job is handed is unchanged — it still reconciles
#: every trip it is told to; only what it buys events for narrows.
#: Six hours. Merging cannot change a metric, so this is chosen purely on cost,
#: and the two costs pull in opposite directions: a wider window costs pages, a
#: narrower one costs requests. Production says requests are the scarce side —
#: one FOXTROT registration emits ~21 events/hour against a ~978-event page, so a
#: window has to span roughly two days before it needs a second page, while
#: every unmerged window is a whole request. Measured over the real 16-day FOXTROT
#: window, widening 60m -> 360m takes a one-day catch-up from 400 requests to
#: 210 and a three-day catch-up from 1,762 to 780, at no extra pages.
VEHICLE_EVENTS_DEFAULT_CANDIDATE_COALESCE_MINUTES = 360

#: Above this many candidate windows the scoped path stops being the cheap one.
#: One window is at least one request, so a dense candidate set — a cold start,
#: a mass backfill, a provider outage that returns a fortnight at once — can
#: cost more requests than the single fleet scan it was meant to avoid: with
#: every FOXTROT trip new, scoping models at 4,528 requests against a fleet scan's
#: ~2,590. The guard is not a cap that truncates work; it selects the other
#: complete strategy. Both remain correct, so this can only ever trade cost.
VEHICLE_EVENTS_DEFAULT_CANDIDATE_MAX_WINDOWS = 1200

VEHICLE_EVENTS_SCOPE_WINDOW = "window"
VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES = "reconciliation_candidates"
SUPPORTED_VEHICLE_EVENTS_SCOPES = frozenset({
    VEHICLE_EVENTS_SCOPE_WINDOW,
    VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
})

#: Scope is a property of the schedule *role*, not of the client. A base run
#: keeps the historical contract; a reconciliation run — whatever its cadence —
#: only owes metrics to the trips it actually discovered. Unknown roles fall
#: back to `window`, so a role added later cannot silently start skipping
#: enrichment before anyone has reasoned about it.
VEHICLE_EVENTS_SCOPE_BY_RUN_TYPE = {
    "DAILY": VEHICLE_EVENTS_SCOPE_WINDOW,
    "WEEKLY_RECONCILIATION": VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
    "MONTHLY_RECONCILIATION": VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
}

VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES = 5
VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES = 5
VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MAX_GAPS_PER_RUN = 1000
VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MAX_SPLIT_DEPTH = 8
VEHICLE_EVENTS_FALLBACK_PROGRESS_INTERVAL = 50
TRIPS_DEFAULT_CHUNK_DAYS = 2
TRIPS_MAX_CHUNK_DAYS = 5
TRIPS_CHUNK_BOUNDARY_STEP = timedelta(seconds=1)
# Row-tuple positions inside every `client_trips` INSERT this job emits. The
# leading 28-column block is identical in both metric variants (the conditional
# metric/speeding fragments are appended after it), so these positions are
# stable. `test_client_trips_distance_cap.py` re-derives both from the INSERT
# column list parsed out of this file, for each variant, so they cannot drift.
CLIENT_TRIPS_PROVIDER_TRIP_ID_VALUE_INDEX = 2
CLIENT_TRIPS_DISTANCE_VALUE_INDEX = 27

TRIP_PARSE_DIAGNOSTIC_COUNTER_KEYS = (
    "trips_provider_rows_fetched",
    "trips_rows_parsed",
    "trips_rows_skipped_missing_registration",
    "trips_rows_malformed_trip_id",
    "trips_rows_malformed_timestamp",
    "trips_rows_other_parse_error",
    client_trips_admission.REJECTED_COUNTER_KEY,
    "client_trips_rejected_distance_over_2000km_at_persistence",
    "trips_rows_prepared_for_upsert",
    "trips_rows_upserted",
    "trips_private_count",
    "trips_business_count",
    "trips_unknown_mode_count",
)


def _elapsed_seconds(started_at: float) -> float:
    return round(max(0.0, time.monotonic() - started_at), 3)


def _records_per_second(count: int, elapsed_s: float) -> Optional[float]:
    if elapsed_s <= 0:
        return None
    return round(count / elapsed_s, 3)


def _provider_metric_delta(before: Dict[str, Any], after: Dict[str, Any], key: str, endpoint: str) -> float:
    before_bucket = before.get(key) or {}
    after_bucket = after.get(key) or {}
    return float(after_bucket.get(endpoint, 0.0)) - float(before_bucket.get(endpoint, 0.0))


def _provider_metric_count_delta(before: Dict[str, Any], after: Dict[str, Any], endpoint: str) -> int:
    before_bucket = before.get("request_count_by_endpoint") or {}
    after_bucket = after.get("request_count_by_endpoint") or {}
    return int(after_bucket.get(endpoint, 0)) - int(before_bucket.get(endpoint, 0))


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _parse_runner_iso_ts(ts: str) -> datetime:
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _positive_int_env_with_source(name: str, default: int) -> Tuple[int, bool]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default, False
    try:
        value = int(raw)
    except ValueError:
        return default, False
    return max(1, value), True


def _vehicle_events_fleet_limit() -> int:
    return min(
        VEHICLE_EVENTS_FLEET_MAX_LIMIT,
        _positive_int_env(VEHICLE_EVENTS_FLEET_LIMIT_ENV, VEHICLE_EVENTS_FLEET_DEFAULT_LIMIT),
    )


def _vehicle_events_fleet_max_pages() -> int:
    return _positive_int_env(VEHICLE_EVENTS_FLEET_MAX_PAGES_ENV, VEHICLE_EVENTS_FLEET_DEFAULT_MAX_PAGES)


def _vehicle_events_chunk_delta() -> timedelta:
    """Nominal fleet-event request chunk, clamped to the wire-safe maximum.

    `/vehicles/events` request windows go on the wire as Europe/Warsaw
    wall-clock, and DST widening can add up to one hour per side. The clamp
    keeps every chunk this job produces below the provider's 24-hour limit
    *after* widening, so a misconfigured `TELEMATICS_EVENTS_CHUNK_HOURS` is split
    rather than rejected by `vehicle_events_wire_window` mid-run. The default
    (4 h) is two orders of magnitude below the clamp; this is defensive only.
    """
    hours = _positive_float_env(VEHICLE_EVENTS_CHUNK_HOURS_ENV, float(VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS))
    return min(timedelta(hours=hours), VEHICLE_EVENTS_MAX_INTENDED_WINDOW)


def _vehicle_events_min_chunk_delta() -> timedelta:
    minutes = _positive_float_env(
        VEHICLE_EVENTS_MIN_CHUNK_MINUTES_ENV,
        float(VEHICLE_EVENTS_DEFAULT_MIN_CHUNK_MINUTES),
    )
    return timedelta(minutes=minutes)


def _vehicle_events_candidate_coalesce_gap() -> timedelta:
    """How close two candidate intervals must be before they share one request.

    Purely a request-count dial. Merging can only ever fetch a superset of the
    deciding events, and every extra event it returns still has to pass the same
    `trip.start_ts <= event.event_ts <= trip.end_ts` test, so no value of this
    setting can change a metric. It trades page volume against request count:
    too small and two trips an hour apart cost two requests, too large and one
    request drags back a day of a busy vehicle's telemetry.
    """
    minutes = _positive_float_env(
        VEHICLE_EVENTS_CANDIDATE_COALESCE_MINUTES_ENV,
        float(VEHICLE_EVENTS_DEFAULT_CANDIDATE_COALESCE_MINUTES),
    )
    return timedelta(minutes=minutes)


def _vehicle_events_candidate_max_windows() -> int:
    return _positive_int_env(
        VEHICLE_EVENTS_CANDIDATE_MAX_WINDOWS_ENV,
        VEHICLE_EVENTS_DEFAULT_CANDIDATE_MAX_WINDOWS,
    )


def _vehicle_events_timeout_s(safety_limits: SafetyLimits) -> int:
    return _positive_int_env(VEHICLE_EVENTS_TIMEOUT_S_ENV, safety_limits.timeout_s)


def _vehicle_events_rate_limit_rps() -> float:
    return _positive_float_env(VEHICLE_EVENTS_RATE_LIMIT_RPS_ENV, VEHICLE_EVENTS_DEFAULT_RATE_LIMIT_RPS)


@dataclass(frozen=True)
class RegistrationFallbackConfig:
    enabled: bool
    rps: float
    max_registrations: int
    max_chunks: int
    max_requests_per_run: int
    max_requests_per_run_explicit: bool
    min_chunk_delta: timedelta
    progress_interval: int = VEHICLE_EVENTS_FALLBACK_PROGRESS_INTERVAL


@dataclass(frozen=True)
class BestEffortConfig:
    min_fleet_chunk_delta: timedelta
    min_registration_chunk_delta: timedelta
    max_gaps_per_run: int
    max_split_depth: int


@dataclass(frozen=True)
class TripFetchChunk:
    index: int
    total: int
    request_start_ts: datetime
    request_end_ts: datetime
    exclusive_end_ts: datetime


def _registration_fallback_config(safety_limits: SafetyLimits) -> RegistrationFallbackConfig:
    max_requests, explicit = _positive_int_env_with_source(
        VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_REQUESTS_PER_RUN_ENV,
        safety_limits.max_requests_per_run,
    )
    return RegistrationFallbackConfig(
        enabled=_bool_env(VEHICLE_EVENTS_ENABLE_REGISTRATION_FALLBACK_ENV, False),
        rps=_positive_float_env(
            VEHICLE_EVENTS_REGISTRATION_FALLBACK_RPS_ENV,
            VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_RPS,
        ),
        max_registrations=_positive_int_env(
            VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_REGISTRATIONS_ENV,
            VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MAX_REGISTRATIONS,
        ),
        max_chunks=_positive_int_env(
            VEHICLE_EVENTS_REGISTRATION_FALLBACK_MAX_CHUNKS_ENV,
            VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MAX_CHUNKS,
        ),
        max_requests_per_run=max_requests,
        max_requests_per_run_explicit=explicit,
        min_chunk_delta=timedelta(
            minutes=_positive_float_env(
                VEHICLE_EVENTS_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES_ENV,
                VEHICLE_EVENTS_DEFAULT_REGISTRATION_FALLBACK_MIN_CHUNK_MINUTES,
            )
        ),
    )


def _best_effort_config() -> BestEffortConfig:
    return BestEffortConfig(
        min_fleet_chunk_delta=timedelta(
            minutes=_positive_float_env(
                VEHICLE_EVENTS_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES_ENV,
                VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MIN_FLEET_CHUNK_MINUTES,
            )
        ),
        min_registration_chunk_delta=timedelta(
            minutes=_positive_float_env(
                VEHICLE_EVENTS_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES_ENV,
                VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MIN_REGISTRATION_CHUNK_MINUTES,
            )
        ),
        max_gaps_per_run=_positive_int_env(
            VEHICLE_EVENTS_BEST_EFFORT_MAX_GAPS_PER_RUN_ENV,
            VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MAX_GAPS_PER_RUN,
        ),
        max_split_depth=_positive_int_env(
            VEHICLE_EVENTS_BEST_EFFORT_MAX_SPLIT_DEPTH_ENV,
            VEHICLE_EVENTS_DEFAULT_BEST_EFFORT_MAX_SPLIT_DEPTH,
        ),
    )


def _raw_event_enrichment_mode(params: dict) -> str:
    raw = params.get("event_enrichment_mode")
    if raw is None or str(raw).strip() == "":
        raw = os.getenv(VEHICLE_EVENTS_ENRICHMENT_MODE_ENV, VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED)
    return str(raw).strip().lower()


def _event_enrichment_mode(params: dict) -> str:
    mode = _raw_event_enrichment_mode(params)
    if mode in {
        VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED,
        VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT,
        VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
    }:
        return VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED
    if mode == VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED:
        return VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED
    raise ValueError(
        "event_enrichment_mode must be one of: "
        f"{VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED}, "
        f"{VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED}; "
        "legacy compatibility values are also accepted: "
        f"{VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT}, "
        f"{VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT}"
    )


def _event_fetch_strategy(params: dict) -> str:
    mode = _raw_event_enrichment_mode(params)
    if mode == VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT:
        return VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT
    if mode in {
        VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED,
        VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED,
        VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT,
    }:
        return VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT
    _event_enrichment_mode(params)
    return VEHICLE_EVENTS_ENRICHMENT_MODE_STRICT


def _vehicle_events_scope(params: dict) -> str:
    """Resolve the event scope for this run, deny-by-default on nonsense.

    An explicit `vehicle_events_scope` wins, so a recovery or audit tool can
    state the contract it wants instead of impersonating a schedule role.
    Otherwise the role decides, and an absent role means `window` — the
    historical behaviour — because a caller that does not say what it is must
    not be quietly given the cheaper contract.
    """
    explicit = params.get("vehicle_events_scope")
    if explicit is not None and str(explicit).strip() != "":
        scope = str(explicit).strip().lower()
        if scope not in SUPPORTED_VEHICLE_EVENTS_SCOPES:
            raise ValueError(
                "vehicle_events_scope must be one of: "
                f"{', '.join(sorted(SUPPORTED_VEHICLE_EVENTS_SCOPES))}"
            )
        return scope
    run_type = str(params.get("schedule_run_type") or "").strip().upper()
    return VEHICLE_EVENTS_SCOPE_BY_RUN_TYPE.get(run_type, VEHICLE_EVENTS_SCOPE_WINDOW)


def _build_candidate_event_windows(
    *,
    candidate_trips: List[Dict[str, Any]],
    registrations_by_norm: Dict[str, str],
    coalesce_gap: timedelta,
) -> List[Dict[str, Any]]:
    """The smallest per-registration windows that still decide every candidate.

    Sufficiency is not a heuristic here, it is the matching rule read backwards.
    `_compute_rpm_vehicle_event_counts` and the speeding pass both count an
    event for a trip only when `trip.start_ts <= event.event_ts <= trip.end_ts`,
    so an event outside a trip's own interval cannot change that trip's metrics
    no matter what else is true about it. Fetching `[start_ts, end_ts]` for the
    trip's registration is therefore not an approximation of the full-window
    fetch — for these trips it is the same set of deciding events.

    No padding is added, deliberately. Padding would only widen an interval
    whose edges are already the exact comparison bounds, and a wider window
    costs pages. Adjacent intervals are merged when they are within
    `coalesce_gap`, which is a request-count optimisation and cannot change a
    result: merging only ever fetches a superset, and every extra event it
    brings back fails the same `start_ts <= ts <= end_ts` test.

    Grouping is per normalised registration because that is the identity the
    provider filters on, and because the fleet path already discards every event
    whose registration is outside the trip registration set.
    """
    by_registration: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for trip in candidate_trips:
        start_ts = trip.get("start_ts")
        end_ts = trip.get("end_ts")
        reg_norm = _normalize_registration(trip.get("registration"))
        if start_ts is None or end_ts is None or end_ts < start_ts or not reg_norm:
            # Undecidable by the matching rule itself: it counts nothing for a
            # trip with no interval or no identity, so there is nothing to buy.
            continue
        by_registration.setdefault(reg_norm, []).append((start_ts, end_ts))

    windows: List[Dict[str, Any]] = []
    for reg_norm in sorted(by_registration):
        intervals = sorted(by_registration[reg_norm])
        merged: List[List[datetime]] = []
        for start_ts, end_ts in intervals:
            if merged and start_ts - merged[-1][1] <= coalesce_gap:
                if end_ts > merged[-1][1]:
                    merged[-1][1] = end_ts
                continue
            merged.append([start_ts, end_ts])
        for start_ts, end_ts in merged:
            windows.append({
                "registration_norm": reg_norm,
                "registration": registrations_by_norm.get(reg_norm, reg_norm),
                "start_ts": start_ts,
                "end_ts": end_ts,
            })
    return windows


def _trips_chunk_days(params: dict) -> int:
    raw = params.get("chunk_days", TRIPS_DEFAULT_CHUNK_DAYS)
    if isinstance(raw, bool):
        raise ValueError(
            f"chunk_days must be an integer between 1 and {TRIPS_MAX_CHUNK_DAYS}; got {raw!r}"
        )
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(
            f"chunk_days must be an integer between 1 and {TRIPS_MAX_CHUNK_DAYS}; got {raw!r}"
        )
    try:
        value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"chunk_days must be an integer between 1 and {TRIPS_MAX_CHUNK_DAYS}; got {raw!r}"
        )
    if value <= 0:
        raise ValueError("chunk_days must be > 0")
    if value > TRIPS_MAX_CHUNK_DAYS:
        raise ValueError(f"chunk_days must be <= {TRIPS_MAX_CHUNK_DAYS}")
    return value


def _trips_pagination_mode(params: dict) -> str:
    """Resolve the frozen `/trips` pagination mode supplied by the caller.

    The dispatcher (compatibility fire) and the C11 recovery runner both emit
    `trips_pagination_mode` in the job params. An absent parameter means the
    caller had no compatibility decision to hand over and resolves to the
    default `strict_meta`; anything else is normalized and fails closed.

    The resolved mode is passed only to the provider client, where it selects
    the `/trips` pagination implementation. No other endpoint is affected.
    """
    if "trips_pagination_mode" not in params:
        return TRIPS_PAGINATION_MODE_DEFAULT
    raw = params.get("trips_pagination_mode")
    if raw is None:
        return TRIPS_PAGINATION_MODE_DEFAULT
    return normalize_trips_pagination_mode(raw)


def _param_bool(params: dict, key: str, default: bool = False) -> bool:
    if key not in params:
        return default
    value = params.get(key)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _parse_provider_dt(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc)

    raw = str(ts).strip()
    if not raw or raw.lower() == "null":
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            dt = datetime.fromisoformat(raw.replace(" ", "T"))

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _normalize_registration(value: Any) -> str:
    """Canonicalize a registration string for matching.

    Provider payloads occasionally return registrations with stray
    whitespace or mixed case (e.g. " WX1234A " vs "wx1234a"). For
    matching purposes we always upper-case and strip; an empty result
    means "no registration available". Never raises — `None` and other
    non-strings collapse to ``""`` which the callers treat as a miss.
    """
    if value is None:
        return ""
    return str(value).strip().upper()


def _normalize_vehicle_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_lookup_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().upper().split())


def _normalize_driver_id(value: Any) -> str:
    return _normalize_lookup_text(value)


def _driver_name_key(first_name: Any, last_name: Any) -> str:
    parts = [
        _normalize_lookup_text(first_name),
        _normalize_lookup_text(last_name),
    ]
    return " ".join(part for part in parts if part)


def _safe_int(val: Any) -> Optional[int]:
    """Parse an int from a provider field; return None on any failure.

    Used for odometer columns (BIGINT) which the provider may emit as
    int, str, or omit entirely. `float` inputs are coerced via int()
    after rounding to be defensive against odometer values reported as
    floats with a trailing ``.0``. Negative or NaN values collapse to
    None — they are never legitimate odometer readings.
    """
    if val is None:
        return None
    if isinstance(val, bool):
        # Python bools are ints; reject to avoid silently storing 0/1.
        return None
    if isinstance(val, int):
        return val if val >= 0 else None
    if isinstance(val, float):
        if val != val or val < 0:  # NaN or negative
            return None
        try:
            return int(val)
        except (OverflowError, ValueError):
            return None
    try:
        s = str(val).strip()
        if not s:
            return None
        # int(str) will reject "12.0"; route floats through float() first.
        if "." in s or "e" in s.lower():
            f = float(s)
            if f != f or f < 0:
                return None
            return int(f)
        n = int(s)
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def _extract_odometer_value(trip: Dict[str, Any], *keys: str) -> Optional[int]:
    """Pick the first non-None odometer value across known field name
    variants and parse it via `_safe_int`.

    Provider payloads in the wild use both ``start_odometer_value`` /
    ``end_odometer_value`` (the canonical names per the spec) and
    legacy synonyms like ``start_odometer`` / ``odometer_start``.
    Trying the canonical name first preserves the documented contract;
    the synonyms are a defense against silent provider drift. Field is
    left NULL if none of the candidates yield a valid value.
    """
    for k in keys:
        if k in trip and trip[k] is not None:
            n = _safe_int(trip[k])
            if n is not None:
                return n
    return None


def _extract_optional_text(payload: Dict[str, Any], *keys: str) -> Optional[str]:
    """Return the first non-empty provider text/id field as a stripped string."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _optional_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw == 1:
            return True
        if raw == 0:
            return False
    return None


def _trip_mode_from_provider(trip: Dict[str, Any]) -> Optional[str]:
    """Map Telematics /trips is_private to the persisted business mode.

    The OpenAPI spec distinguishes `is_private` (telematics-derived) from
    user-editable `trip_type`. We intentionally use only `is_private` here.
    """
    is_private = _optional_bool(trip.get("is_private"))
    if is_private is True:
        return "private"
    if is_private is False:
        return "business"
    return None


def _provider_payload_keys(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return []
    return sorted(str(k) for k in payload.keys())


def _trip_diagnostic_context(
    payload: Any,
    *,
    reason: str,
    provider_trip_id: Any = None,
    error: Optional[BaseException] = None,
) -> Dict[str, Any]:
    context = {
        "reason": reason,
        "provider_trip_id": provider_trip_id,
        "registration": payload.get("registration") if isinstance(payload, dict) else None,
        "vehicle_id": payload.get("vehicle_id") if isinstance(payload, dict) else None,
        "raw_payload_keys": _provider_payload_keys(payload),
    }
    if error is not None:
        context["error_type"] = type(error).__name__
        context["error_detail"] = str(error)
    if not isinstance(payload, dict):
        context["raw_payload_type"] = type(payload).__name__
    return context


def _build_vehicle_metadata_lookups(
    vehicles: Iterable[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Optional[str]]], Dict[str, Dict[str, Optional[str]]]]:
    by_vehicle_id: Dict[str, Dict[str, Optional[str]]] = {}
    by_registration: Dict[str, Dict[str, Optional[str]]] = {}

    for vehicle in vehicles:
        metadata = {
            "vehicle_name": _extract_optional_text(vehicle, "vehicle_name"),
            "vehicle_description": _extract_optional_text(vehicle, "vehicle_description"),
        }
        vehicle_id_norm = _normalize_vehicle_id(vehicle.get("vehicle_id"))
        if vehicle_id_norm and vehicle_id_norm not in by_vehicle_id:
            by_vehicle_id[vehicle_id_norm] = metadata

        registration_norm = _normalize_registration(vehicle.get("registration"))
        if registration_norm and registration_norm not in by_registration:
            by_registration[registration_norm] = metadata

    return by_vehicle_id, by_registration


def _vehicle_metadata_for_trip(
    *,
    vehicle_id: Any,
    registration: Any,
    by_vehicle_id: Dict[str, Dict[str, Optional[str]]],
    by_registration: Dict[str, Dict[str, Optional[str]]],
) -> Dict[str, Optional[str]]:
    vehicle_id_norm = _normalize_vehicle_id(vehicle_id)
    if vehicle_id_norm and vehicle_id_norm in by_vehicle_id:
        return by_vehicle_id[vehicle_id_norm]

    registration_norm = _normalize_registration(registration)
    if registration_norm and registration_norm in by_registration:
        return by_registration[registration_norm]

    return {"vehicle_name": None, "vehicle_description": None}


def _build_driver_restriction_lookups(
    drivers: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Optional[str]]]:
    by_driver_id: Dict[str, Optional[str]] = {}
    by_identification_tag_id: Dict[str, Optional[str]] = {}
    by_name: Dict[str, Optional[str]] = {}

    for driver in drivers:
        restrictions = _extract_optional_text(driver, "license_driver_restrictions")

        driver_id_norm = _normalize_driver_id(driver.get("driver_id"))
        if driver_id_norm and driver_id_norm not in by_driver_id:
            by_driver_id[driver_id_norm] = restrictions

        tag_id_norm = _normalize_lookup_text(driver.get("identification_tag_id"))
        if tag_id_norm and tag_id_norm not in by_identification_tag_id:
            by_identification_tag_id[tag_id_norm] = restrictions

        name_key = _driver_name_key(driver.get("first_name"), driver.get("last_name"))
        if name_key and name_key not in by_name:
            by_name[name_key] = restrictions

    return {
        "by_driver_id": by_driver_id,
        "by_identification_tag_id": by_identification_tag_id,
        "by_name": by_name,
    }


def _driver_restrictions_for_trip(
    *,
    driver_id: Any,
    identification_tag_id: Any,
    driver_name: Any,
    driver_surname: Any,
    lookups: Dict[str, Dict[str, Optional[str]]],
) -> Tuple[Optional[str], str]:
    driver_id_norm = _normalize_driver_id(driver_id)
    if driver_id_norm and driver_id_norm in lookups["by_driver_id"]:
        return lookups["by_driver_id"][driver_id_norm], "driver_id"

    tag_id_norm = _normalize_lookup_text(identification_tag_id)
    if tag_id_norm and tag_id_norm in lookups["by_identification_tag_id"]:
        return lookups["by_identification_tag_id"][tag_id_norm], "identification_tag_id"

    name_key = _driver_name_key(driver_name, driver_surname)
    if name_key and name_key in lookups["by_name"]:
        return lookups["by_name"][name_key], "driver_name"

    if driver_id_norm or tag_id_norm or name_key:
        return None, "unmatched"
    return None, "unidentified"


def _speed_bucket(speed_raw: Any) -> Optional[str]:
    if speed_raw is None:
        return None
    try:
        s = float(speed_raw)
    except (TypeError, ValueError):
        return None

    if 140 <= s < 160:
        return "speeding_140_160_count"
    if 160 <= s < 170:
        return "speeding_160_170_count"
    if s >= 170:
        return "speeding_170_plus_count"
    return None


def _uuid_from_md5_deterministic(s: str) -> uuid.UUID:
    digest = hashlib.md5(s.encode("utf-8")).digest()
    return uuid.UUID(bytes=digest)


def _extract_provider_notification_id(notification: Dict[str, Any]) -> Optional[str]:
    for key in ("notification_id", "alert_id", "id"):
        if key in notification and notification[key] is not None:
            try:
                return str(uuid.UUID(str(notification[key])))
            except Exception:
                pass
    return None


# RPM-related notification types we care about for client_trips counts.
# Kept upper-case; the extractor canonicalizes raw provider values.
_RPM_TYPE_HIGH_RPM = "HIGH_RPM"
_RPM_TYPE_OVERREV = "OVERREV"
_RPM_EVENT_PHASE_START = "START"
_RPM_EVENT_PHASE_END = "END"
_RPM_EVENT_PHASE_UNSPECIFIED = "UNSPECIFIED"
_RPM_VEHICLE_EVENT_TEXT_FIELDS = (
    "event_description",
    "description",
    "type",
    "event_type",
    "alert_type",
    "notification_type",
    "trigger_description",
    "message",
    "notification_msg",
)


def _normalize_notification_type_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.upper().replace("-", "_")
    normalized = "_".join(normalized.split())
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


def _canonical_rpm_notification_type(value: Any) -> Optional[str]:
    normalized = _normalize_notification_type_value(value)
    if normalized is None:
        return None
    compact = normalized.replace("_", "")
    if "HIGH_RPM" in normalized or "HIGHRPM" in compact:
        return _RPM_TYPE_HIGH_RPM
    if "OVERREV" in compact:
        return _RPM_TYPE_OVERREV
    return None


def _canonical_rpm_vehicle_event_label(value: Any) -> Optional[Tuple[str, str]]:
    """Return (RPM type, phase) for provider-labeled vehicle-event rows.

    Workflow A counts only provider labels, never numeric `rpm` thresholds.
    START rows and unsuffixed labels count as one event; END rows are
    recognized but ignored to avoid double-counting a single provider incident.
    """
    normalized = _normalize_notification_type_value(value)
    if normalized is None:
        return None

    compact = normalized.replace("_", "")
    if "HIGHRPM" in compact:
        rpm_type = _RPM_TYPE_HIGH_RPM
    elif "OVERREV" in compact:
        rpm_type = _RPM_TYPE_OVERREV
    else:
        return None

    tokens = set(normalized.split("_"))
    if "END" in tokens or compact.endswith("END"):
        return rpm_type, _RPM_EVENT_PHASE_END
    if "START" in tokens or compact.endswith("START"):
        return rpm_type, _RPM_EVENT_PHASE_START
    return rpm_type, _RPM_EVENT_PHASE_UNSPECIFIED


def _extract_vehicle_event_rpm_label(event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else event
    for key in _RPM_VEHICLE_EVENT_TEXT_FIELDS:
        label = _canonical_rpm_vehicle_event_label(raw.get(key))
        if label is not None:
            return label
    return None


def _extract_notification_type(notification: Dict[str, Any]) -> Optional[str]:
    """Best-effort canonical notification type for RPM matching.

    Telematics documents `trigger_description` as the notification alert type,
    but payloads may also carry generic fields such as `type`. Explicit
    RPM/OVERREV labels therefore win across all likely fields before falling
    back to the first generic type value for histogram/unknown-type logging.
    """
    type_fields = (
        "trigger_description",
        "type",
        "notification_type",
        "event_type",
        "alert_type",
        "event_description",
        "description",
        "notification_msg",
    )
    for key in type_fields:
        rpm_type = _canonical_rpm_notification_type(notification.get(key))
        if rpm_type is not None:
            return rpm_type

    for key in ("type", "notification_type", "event_type", "alert_type", "trigger_description"):
        v = notification.get(key)
        normalized = _normalize_notification_type_value(v)
        if normalized is not None:
            return normalized
    return None


def _derive_notification_uuid(notification: Dict[str, Any], *, event_ts: Optional[datetime]) -> str:
    registration = str(notification.get("registration") or "")
    trigger_description = str(notification.get("trigger_description") or "")
    speed = notification.get("speed")
    geofence_id = str(notification.get("geofence_id") or "")
    notification_msg = str(notification.get("notification_msg") or "")

    ts_s = event_ts.isoformat() if event_ts is not None else ""
    raw = "|".join([registration, trigger_description, str(speed), geofence_id, notification_msg, ts_s])
    return str(_uuid_from_md5_deterministic(raw))


def _notification_dedupe_key(notification: Dict[str, Any]) -> str:
    """Stable identity for a notification, used to dedupe per-trip
    matches in `_compute_rpm_counts`.

    Why not list index?
        The previous implementation used the notification's position in
        the input list (`enumerate(notifications)`) as the identity for
        per-trip dedup. That works in unit tests (the input list is
        fixed) but is **fragile in production**: notifications are
        fetched paginated, in 31-day sub-windows, and the provider does
        not guarantee stable ordering between runs. The same logical
        event landing at index 7 in one run and index 11 in the next
        would dedupe correctly inside a single run, but the choice of
        which "duplicate" wins (when the same event matches via both
        vehicle_id and registration) could drift between runs. A
        content-addressable key keeps the math invariant under
        reordering and makes the matching layer deterministic on
        identity alone.

    Identity rules (in order):
      1. `provider_notification_id` if non-empty — already a UUID
         either extracted from the provider's stable id (one of
         ``notification_id`` / ``alert_id`` / ``id``) or computed by
         `_derive_notification_uuid` from a deterministic content hash.
         This is the strongest identity available and the canonical
         per-row key on `client_speeding_notifications`.
      2. Otherwise, a deterministic composite of the matching-relevant
         fields: canonical type, vehicle_id, normalized registration,
         event_ts ISO timestamp (UTC), and (when present) one of
         ``notification_msg`` or ``status`` for an extra
         disambiguator. Two events with the same (type, vehicle, ts)
         but different msg/status stay distinct.

    Returns a string. The caller only requires hashability and
    equality, so we keep the cheap string path rather than UUID
    canonicalization. The ``pid:`` / ``fb:`` prefix prevents an
    accidental collision between a UUID-shaped pid and a fallback-key
    string that happens to look UUID-shaped.
    """
    pid = notification.get("provider_notification_id")
    if pid is not None:
        s = str(pid).strip()
        if s:
            return f"pid:{s}"

    ntype = notification.get("type")
    type_part = str(ntype).strip().upper() if ntype is not None else ""

    vid = notification.get("vehicle_id")
    vid_part = str(vid) if vid is not None else ""

    reg_part = _normalize_registration(notification.get("registration"))

    ets = notification.get("event_ts")
    if isinstance(ets, datetime):
        ts_part = ets.astimezone(timezone.utc).isoformat()
    elif ets is None:
        ts_part = ""
    else:
        ts_part = str(ets)

    msg = notification.get("notification_msg")
    if msg is None:
        msg = notification.get("status")
    msg_part = str(msg).strip() if msg is not None else ""

    return "fb:" + "|".join((type_part, vid_part, reg_part, ts_part, msg_part))


def _client_business_pg_conn(cfg: ClientAccountConfig):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _window_business_context(window_start_ts: datetime, window_end_ts: datetime) -> dict[str, str]:
    return {
        "window_start_ts_local": format_business_timestamp(window_start_ts),
        "window_end_ts_local": format_business_timestamp(window_end_ts),
        "timezone": get_business_timezone_name(),
    }


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_coords(coords: Any) -> Tuple[Optional[float], Optional[float]]:
    """Extract (latitude, longitude) from a provider coordinates object."""
    if not isinstance(coords, dict):
        return None, None
    return _safe_float(coords.get("latitude")), _safe_float(coords.get("longitude"))


@dataclass(frozen=True)
class TripForMatching:
    provider_trip_id: int
    registration: str
    start_ts: datetime
    end_ts: datetime
    width_seconds: int


@dataclass(frozen=True)
class SpeedingViolation:
    registration: str
    event_ts: datetime
    speed: float
    bucket: str


@dataclass(frozen=True)
class RpmVehicleEvent:
    rpm_type: str
    registration: str
    vehicle_id: str
    event_ts: datetime
    dedupe_key: str


def _rpm_vehicle_event_dedupe_key(event: Dict[str, Any], *, rpm_type: str, event_ts: datetime) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    event_id = event.get("event_id")
    if event_id is None:
        event_id = raw.get("event_id") or raw.get("id")
    if event_id is not None and str(event_id).strip():
        return f"eid:{str(event_id).strip()}"

    event_description = str(raw.get("event_description") or "")
    return "fb:" + "|".join((
        rpm_type,
        _normalize_vehicle_id(event.get("vehicle_id")),
        _normalize_registration(event.get("registration")),
        event_ts.astimezone(timezone.utc).isoformat(),
        event_description,
    ))


def _filter_fleet_rpm_vehicle_events(
    *,
    vehicle_events: List[Dict[str, Any]],
    trip_registrations_norm: set[str],
    trip_vehicle_ids_norm: set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    identity_kept: List[Dict[str, Any]] = []
    labeled_kept: List[Dict[str, Any]] = []

    for event in vehicle_events:
        reg_norm = _normalize_registration(event.get("registration"))
        vehicle_id_norm = _normalize_vehicle_id(event.get("vehicle_id"))
        if reg_norm not in trip_registrations_norm and vehicle_id_norm not in trip_vehicle_ids_norm:
            continue
        identity_kept.append(event)

        if _extract_vehicle_event_rpm_label(event) is None:
            continue
        labeled_kept.append(event)

    stats = {
        "rows_fetched": len(vehicle_events),
        "rows_kept_after_trip_identity_filter": len(identity_kept),
        "provider_labeled_rpm_rows_kept": len(labeled_kept),
    }
    return labeled_kept, stats


def _build_rpm_vehicle_events(
    vehicle_events: List[Dict[str, Any]],
) -> Tuple[List[RpmVehicleEvent], Dict[str, int]]:
    rpm_events: List[RpmVehicleEvent] = []
    label_rows_seen = 0
    high_rpm_label_rows_seen = 0
    overrev_label_rows_seen = 0
    high_rpm_end_events_ignored = 0
    overrev_end_events_ignored = 0
    missing_event_ts = 0
    missing_match_identity = 0

    for event in vehicle_events:
        label = _extract_vehicle_event_rpm_label(event)
        if label is None:
            continue
        rpm_type, phase = label
        label_rows_seen += 1
        if rpm_type == _RPM_TYPE_HIGH_RPM:
            high_rpm_label_rows_seen += 1
        else:
            overrev_label_rows_seen += 1

        if phase == _RPM_EVENT_PHASE_END:
            if rpm_type == _RPM_TYPE_HIGH_RPM:
                high_rpm_end_events_ignored += 1
            else:
                overrev_end_events_ignored += 1
            continue

        event_ts = _parse_provider_dt(event.get("event_ts"))
        if event_ts is None:
            missing_event_ts += 1
            continue

        reg_norm = _normalize_registration(event.get("registration"))
        vehicle_id_norm = _normalize_vehicle_id(event.get("vehicle_id"))
        if not reg_norm and not vehicle_id_norm:
            missing_match_identity += 1
            continue

        rpm_events.append(
            RpmVehicleEvent(
                rpm_type=rpm_type,
                registration=reg_norm,
                vehicle_id=vehicle_id_norm,
                event_ts=event_ts,
                dedupe_key=_rpm_vehicle_event_dedupe_key(
                    event,
                    rpm_type=rpm_type,
                    event_ts=event_ts,
                ),
            )
        )

    rpm_events.sort(key=lambda e: (e.vehicle_id, e.registration, e.event_ts, e.rpm_type))
    stats = {
        "vehicle_events_total": len(vehicle_events),
        "rpm_label_rows_seen": label_rows_seen,
        "high_rpm_label_rows_seen": high_rpm_label_rows_seen,
        "overrev_label_rows_seen": overrev_label_rows_seen,
        "high_rpm_end_events_ignored": high_rpm_end_events_ignored,
        "overrev_end_events_ignored": overrev_end_events_ignored,
        "missing_event_ts": missing_event_ts,
        "missing_match_identity": missing_match_identity,
        "high_rpm_events": sum(1 for event in rpm_events if event.rpm_type == _RPM_TYPE_HIGH_RPM),
        "overrev_events": sum(1 for event in rpm_events if event.rpm_type == _RPM_TYPE_OVERREV),
    }
    return rpm_events, stats


def _compute_rpm_vehicle_event_counts(
    *,
    trips: List[Dict[str, Any]],
    vehicle_events: List[Dict[str, Any]],
) -> Tuple[Dict[int, Dict[str, int]], Dict[str, int]]:
    by_vehicle: Dict[str, List[RpmVehicleEvent]] = {}
    by_registration: Dict[str, List[RpmVehicleEvent]] = {}

    rpm_events, build_stats = _build_rpm_vehicle_events(vehicle_events)
    for event in rpm_events:
        if event.vehicle_id:
            by_vehicle.setdefault(event.vehicle_id, []).append(event)
        if event.registration:
            by_registration.setdefault(event.registration, []).append(event)

    for events in by_vehicle.values():
        events.sort(key=lambda e: e.event_ts)
    for events in by_registration.values():
        events.sort(key=lambda e: e.event_ts)

    counts: Dict[int, Dict[str, int]] = {}
    matched_high_rpm = 0
    matched_overrev = 0
    matches_via_vehicle = 0
    matches_via_registration = 0
    trips_with_events = 0
    trips_without_events = 0

    for t in trips:
        provider_trip_id = int(t["provider_trip_id"])
        rpm = {"high_rpm": 0, "overrev": 0}
        counts[provider_trip_id] = rpm

        start_ts = t.get("start_ts")
        end_ts = t.get("end_ts")
        if start_ts is None or end_ts is None or end_ts < start_ts:
            trips_without_events += 1
            continue

        vid = _normalize_vehicle_id(t.get("vehicle_id"))
        reg = _normalize_registration(t.get("registration"))
        seen_keys: set[str] = set()

        if vid:
            for event in by_vehicle.get(vid, ()):
                if event.event_ts > end_ts:
                    break
                if event.event_ts < start_ts or event.dedupe_key in seen_keys:
                    continue
                seen_keys.add(event.dedupe_key)
                if event.rpm_type == _RPM_TYPE_HIGH_RPM:
                    rpm["high_rpm"] += 1
                    matched_high_rpm += 1
                else:
                    rpm["overrev"] += 1
                    matched_overrev += 1
                matches_via_vehicle += 1

        if reg:
            for event in by_registration.get(reg, ()):
                if event.event_ts > end_ts:
                    break
                if event.event_ts < start_ts or event.dedupe_key in seen_keys:
                    continue
                seen_keys.add(event.dedupe_key)
                if event.rpm_type == _RPM_TYPE_HIGH_RPM:
                    rpm["high_rpm"] += 1
                    matched_high_rpm += 1
                else:
                    rpm["overrev"] += 1
                    matched_overrev += 1
                matches_via_registration += 1

        if rpm["high_rpm"] + rpm["overrev"] > 0:
            trips_with_events += 1
        else:
            trips_without_events += 1

    stats = {
        **build_stats,
        "vehicles_with_events": len(by_vehicle),
        "registrations_with_events": len(by_registration),
        "matched_high_rpm": matched_high_rpm,
        "matched_overrev": matched_overrev,
        "trips_with_events": trips_with_events,
        "trips_without_events": trips_without_events,
        "matches_via_vehicle_id": matches_via_vehicle,
        "matches_via_registration": matches_via_registration,
    }
    return counts, stats


def _build_speeding_violations(
    vehicle_events: List[Dict[str, Any]],
) -> List[SpeedingViolation]:
    violations: List[SpeedingViolation] = []
    for event in vehicle_events:
        event_ts = _parse_provider_dt(event.get("event_ts"))
        if event_ts is None:
            continue

        speed = _safe_float(event.get("speed"))
        if speed is None or speed < 140:
            continue

        reg_norm = _normalize_registration(event.get("registration"))
        if not reg_norm:
            continue

        bucket = _speed_bucket(speed)
        if bucket is None:
            continue
        violations.append(
            SpeedingViolation(
                registration=reg_norm,
                event_ts=event_ts,
                speed=speed,
                bucket=bucket,
            )
        )

    violations.sort(key=lambda v: (v.registration, v.event_ts, v.speed))
    return violations


def _compute_speeding_violation_counts(
    *,
    trips: List[Dict[str, Any]],
    vehicle_events: List[Dict[str, Any]],
) -> Tuple[Dict[int, Dict[str, int]], Dict[str, int]]:
    trips_by_reg: Dict[str, List[TripForMatching]] = {}
    for t in trips:
        start_ts = t.get("start_ts")
        end_ts = t.get("end_ts")
        if start_ts is None or end_ts is None:
            continue
        if end_ts < start_ts:
            continue

        reg_norm = _normalize_registration(t.get("registration"))
        provider_trip_id = int(t["provider_trip_id"])
        width_seconds = int((end_ts - start_ts).total_seconds())
        trips_by_reg.setdefault(reg_norm, []).append(
            TripForMatching(
                provider_trip_id=provider_trip_id,
                registration=reg_norm,
                start_ts=start_ts,
                end_ts=end_ts,
                width_seconds=width_seconds,
            )
        )

    counts: Dict[int, Dict[str, int]] = {}
    for t in trips:
        provider_trip_id = int(t["provider_trip_id"])
        counts[provider_trip_id] = {
            "speeding_140_160_count": 0,
            "speeding_160_170_count": 0,
            "speeding_170_plus_count": 0,
        }

    violations = _build_speeding_violations(vehicle_events)
    matched = 0
    unmatched = 0
    for violation in violations:
        candidates = trips_by_reg.get(violation.registration) or []

        matching: List[TripForMatching] = [
            c for c in candidates if c.start_ts <= violation.event_ts <= c.end_ts
        ]
        if not matching:
            unmatched += 1
            continue

        best = min(matching, key=lambda c: (c.width_seconds, c.start_ts, c.provider_trip_id))
        counts[best.provider_trip_id][violation.bucket] += 1
        matched += 1

    stats = {
        "vehicle_events_total": len(vehicle_events),
        "speeding_violations_created": len(violations),
        "speeding_violations_matched": matched,
        "speeding_violations_unmatched": unmatched,
    }
    return counts, stats


def _filter_fleet_speeding_events(
    *,
    vehicle_events: List[Dict[str, Any]],
    trip_registrations_norm: set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    registration_kept: List[Dict[str, Any]] = []
    speeding_kept: List[Dict[str, Any]] = []

    for event in vehicle_events:
        reg_norm = _normalize_registration(event.get("registration"))
        if reg_norm not in trip_registrations_norm:
            continue
        registration_kept.append(event)

        speed = _safe_float(event.get("speed"))
        if speed is None or speed < 140:
            continue
        speeding_kept.append(event)

    stats = {
        "rows_fetched": len(vehicle_events),
        "rows_kept_after_registration_filter": len(registration_kept),
        "rows_kept_after_speed_filter": len(speeding_kept),
    }
    return speeding_kept, stats


def _build_registration_fallback_list(
    *,
    vehicle_inventory_rows: Iterable[Dict[str, Any]],
    trip_registrations_by_norm: Dict[str, str],
) -> List[str]:
    registrations_by_norm: Dict[str, str] = {}
    for vehicle in vehicle_inventory_rows:
        registration = str(vehicle.get("registration") or "").strip()
        reg_norm = _normalize_registration(registration)
        if reg_norm and reg_norm not in registrations_by_norm:
            registrations_by_norm[reg_norm] = registration

    for reg_norm, registration in trip_registrations_by_norm.items():
        if reg_norm and reg_norm not in registrations_by_norm:
            registrations_by_norm[reg_norm] = str(registration).strip()

    return [
        registrations_by_norm[reg_norm]
        for reg_norm in sorted(registrations_by_norm)
        if registrations_by_norm[reg_norm]
    ]


def _build_trip_fetch_chunks(
    window_start_ts: datetime,
    window_end_ts: datetime,
    *,
    chunk_days: int,
) -> List[TripFetchChunk]:
    start_ts = window_start_ts.astimezone(timezone.utc)
    end_ts = window_end_ts.astimezone(timezone.utc)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")
    if chunk_days > TRIPS_MAX_CHUNK_DAYS:
        raise ValueError(f"chunk_days must be <= {TRIPS_MAX_CHUNK_DAYS}")

    step = timedelta(days=chunk_days)
    raw_chunks: List[Tuple[datetime, datetime, datetime]] = []
    if start_ts == end_ts:
        raw_chunks.append((start_ts, end_ts, end_ts))
    else:
        current = start_ts
        while current < end_ts:
            exclusive_end = min(current + step, end_ts)
            request_end = exclusive_end
            if exclusive_end < end_ts:
                request_end = exclusive_end - TRIPS_CHUNK_BOUNDARY_STEP
            if request_end < current:
                current = exclusive_end
                continue
            raw_chunks.append((current, request_end, exclusive_end))
            current = exclusive_end

    total = len(raw_chunks)
    return [
        TripFetchChunk(
            index=index,
            total=total,
            request_start_ts=request_start,
            request_end_ts=request_end,
            exclusive_end_ts=exclusive_end,
        )
        for index, (request_start, request_end, exclusive_end) in enumerate(raw_chunks, start=1)
    ]


def _persist_pending_request_facts(
    *,
    client: Any,
    run_id: str,
    client_id: str,
    platform_run_id: str,
    completeness,
) -> int:
    """Commit this execution's request facts to the platform DB, PENDING.

    Step 1 of the M4 cross-database handoff, and the reason an immutable
    `client_trips.first_seen_request_id` is safe to write at all: it runs
    **before** the business transaction, on its own platform connection and its
    own commit, so a request identity is durable before any trip row can
    reference it.

    Deliberately fail-loud. If these facts cannot be made durable, the run must
    not go on to commit trips that point at them, so the exception propagates
    and the fire fails with nothing written to the business database. That is
    strictly safer than the alternative it replaces, where the business commit
    happened first and could strand a reference permanently.
    """
    # Named `platform_db`, deliberately sharing no suffix with the job's
    # business `conn`. `ops/tests_manual/test_telematics_trips_pagination_compat.py`
    # pins that the business transaction commits exactly once by counting commit
    # calls in this file's source; a name ending in `conn` would be absorbed
    # into that count and quietly disarm the boundary check.
    platform_db = _platform_pg_conn()
    try:
        written = persist_pending_request_facts(
            platform_db,
            platform_run_id=platform_run_id,
            completeness=completeness,
        )
        platform_db.commit()
    except Exception:
        try:
            platform_db.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            platform_db.close()
        except Exception:
            pass
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Durable provider request facts recorded (PENDING)",
        run_id=run_id,
        context={
            "client_id": client_id,
            "platform_run_id": platform_run_id,
            "request_facts_written": written,
            "endpoint": completeness.endpoint,
            "sub_window_count": len(completeness.subwindows),
            # Stated in the log because it is the property that makes the
            # write safe: these rows carry no fire attribution and no
            # completeness claim, so they cannot advance coverage.
            "coverage_eligible": False,
        },
    )
    return written


def _fetch_trips_in_chunks(
    *,
    telematics: TelematicsFleetProviderClient,
    client: Any,
    run_id: str,
    client_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    chunk_days: int,
    incl_private: bool,
    evidence: Optional[RequestEvidenceCollector] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    chunks = _build_trip_fetch_chunks(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        chunk_days=chunk_days,
    )
    all_trips: List[Dict[str, Any]] = []
    chunk_summaries: List[Dict[str, Any]] = []

    for chunk in chunks:
        # M4: declare this tiling unit before the provider is asked anything.
        # `exclusive_end_ts` — not the inclusive wire end — is what tiles the
        # effective window, and declaring it here is what makes a chunk the loop
        # never reached visible: it is simply absent from the tiling, and the
        # dispatcher's union check then refuses to advance coverage.
        if evidence is not None:
            evidence.begin_tiling_unit(
                index=chunk.index,
                covers_from=chunk.request_start_ts,
                covers_to=chunk.exclusive_end_ts,
                requested_from=chunk.request_start_ts,
                requested_to=chunk.request_end_ts,
            )
        chunk_context = {
            "client_id": client_id,
            "endpoint": "/trips",
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "chunk_start_ts": chunk.request_start_ts.isoformat(),
            "chunk_end_ts": chunk.request_end_ts.isoformat(),
            "chunk_exclusive_end_ts": chunk.exclusive_end_ts.isoformat(),
            "chunk_days": chunk_days,
            "original_window_start_ts": window_start_ts.isoformat(),
            "original_window_end_ts": window_end_ts.isoformat(),
        }
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Fetching provider trips chunk",
            run_id=run_id,
            context={**chunk_context, "incl_private": incl_private},
        )
        chunk_started_at = time.monotonic()
        try:
            chunk_rows = telematics.fetch_trips(
                window_start_ts=chunk.request_start_ts,
                window_end_ts=chunk.request_end_ts,
                incl_private=incl_private,
            )
        except TelematicsProviderSafetyError as exc:
            error_context = {**exc.context, **chunk_context, "abort_code": exc.code, "phase": "fetch_trips"}
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Telematics provider safety stop in /trips chunk {chunk.index}/{chunk.total}: {exc.code}",
                run_id=run_id,
                context=error_context,
            )
            raise TelematicsProviderSafetyError(
                exc.code,
                (
                    f"{exc} while fetching /trips chunk {chunk.index}/{chunk.total} "
                    f"for client_id={client_id} "
                    f"chunk_start_ts={chunk.request_start_ts.isoformat()} "
                    f"chunk_end_ts={chunk.request_end_ts.isoformat()} "
                    f"original_window_start_ts={window_start_ts.isoformat()} "
                    f"original_window_end_ts={window_end_ts.isoformat()}"
                ),
                context=error_context,
            ) from exc
        except Exception as exc:
            error_context = {
                **chunk_context,
                "phase": "fetch_trips",
                "error_type": type(exc).__name__,
                "error_detail": str(exc),
            }
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                f"Provider /trips chunk fetch failed: {type(exc).__name__}",
                run_id=run_id,
                context=error_context,
            )
            raise RuntimeError(
                (
                    f"Failed to fetch /trips chunk {chunk.index}/{chunk.total} "
                    f"for client_id={client_id} "
                    f"chunk_start_ts={chunk.request_start_ts.isoformat()} "
                    f"chunk_end_ts={chunk.request_end_ts.isoformat()} "
                    f"original_window_start_ts={window_start_ts.isoformat()} "
                    f"original_window_end_ts={window_end_ts.isoformat()}"
                )
            ) from exc

        elapsed_s = _elapsed_seconds(chunk_started_at)
        summary = {
            **chunk_context,
            "records_fetched": len(chunk_rows),
            "elapsed_seconds": elapsed_s,
            "records_per_second": _records_per_second(len(chunk_rows), elapsed_s),
        }
        chunk_summaries.append(summary)
        all_trips.extend(chunk_rows)
        if evidence is not None:
            evidence.end_tiling_unit()
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Fetched provider trips chunk",
            run_id=run_id,
            context=summary,
        )

    return all_trips, chunk_summaries


def _iter_vehicle_events_fleet_request_windows(
    window_start_ts: datetime,
    window_end_ts: datetime,
    *,
    chunk_delta: Optional[timedelta] = None,
) -> Iterable[Tuple[datetime, datetime, datetime]]:
    """Yield fixed-size fleet-event request windows.

    The third tuple value is the exclusive chunk end used to advance the next
    request. For non-final chunks, the provider request end is reduced by one
    second to avoid double-counting inclusive boundary timestamps.
    """
    start_ts = window_start_ts.astimezone(timezone.utc)
    end_ts = window_end_ts.astimezone(timezone.utc)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")
    step = chunk_delta or _vehicle_events_chunk_delta()
    if step <= timedelta(0):
        raise ValueError("chunk_delta must be > 0")

    current = start_ts
    while current < end_ts:
        chunk_end = min(current + step, end_ts)
        request_end = chunk_end
        if chunk_end < end_ts:
            request_end = chunk_end - timedelta(seconds=1)

        if request_end <= current:
            current = chunk_end
            continue

        yield current, request_end, chunk_end
        current = chunk_end


def _halve_timedelta(value: timedelta, *, minimum: timedelta) -> timedelta:
    halved = timedelta(seconds=max(1, int(value.total_seconds() // 2)))
    if halved < minimum:
        return minimum
    return halved


def _vehicle_events_error_can_reduce_chunk(exc: TelematicsProviderSafetyError) -> bool:
    if exc.code == "HTTP_RETRY_EXHAUSTED":
        return True
    if exc.code != "HTTP_ERROR":
        return False
    status_code = exc.context.get("status_code")
    try:
        status_int = int(status_code)
    except (TypeError, ValueError):
        return False
    return status_int in {408, 429, 500, 502, 503, 504}


def _vehicle_events_error_can_fallback(exc: TelematicsProviderSafetyError) -> bool:
    if exc.code == "HTTP_RETRY_EXHAUSTED":
        return True
    if exc.code != "HTTP_ERROR":
        return False
    try:
        return int(exc.context.get("status_code")) == 500
    except (TypeError, ValueError):
        return False


def _vehicle_events_error_is_best_effort_gap(exc: TelematicsProviderSafetyError) -> bool:
    if exc.code == "HTTP_RETRY_EXHAUSTED":
        return True
    if exc.code != "HTTP_ERROR":
        return False
    try:
        return int(exc.context.get("status_code")) in {408, 500, 502, 503, 504}
    except (TypeError, ValueError):
        return False


def _vehicle_events_error_is_best_effort_fail_fast(exc: TelematicsProviderSafetyError) -> bool:
    if exc.code == "HTTP_ERROR":
        try:
            return int(exc.context.get("status_code")) in {401, 403, 429}
        except (TypeError, ValueError):
            return False
    return exc.code in {
        "MALFORMED_RESPONSE",
        "MALFORMED_PAGINATION",
        "PAGINATION_MISMATCH",
        "PAGINATION_LOOP",
        "PAGINATION_NON_PROGRESS",
        "INCONSISTENT_PAGINATION",
        "MAX_REQUESTS_PER_RUN",
        "MAX_REQUESTS_PER_ENDPOINT",
        "MAX_REQUESTS_PER_SUBWINDOW",
        "MAX_PAGES_PER_SUBWINDOW",
        "VEHICLE_EVENTS_MAX_PAGES_REACHED",
    }


def _event_window_seconds(start_ts: datetime, end_ts: datetime) -> float:
    return max(0.0, (end_ts - start_ts).total_seconds() + 1.0)


def _iter_inclusive_event_subwindows(
    start_ts: datetime,
    end_ts: datetime,
    *,
    chunk_delta: timedelta,
) -> Iterable[Tuple[datetime, datetime]]:
    if end_ts < start_ts:
        raise ValueError("end_ts must be >= start_ts")
    if chunk_delta <= timedelta(0):
        raise ValueError("chunk_delta must be > 0")

    current = start_ts
    exclusive_end = end_ts + timedelta(seconds=1)
    while current < exclusive_end:
        chunk_exclusive_end = min(current + chunk_delta, exclusive_end)
        request_end = chunk_exclusive_end - timedelta(seconds=1)
        if request_end >= current:
            yield current, request_end
        current = chunk_exclusive_end


def _estimated_chunk_total(current: datetime, end_ts: datetime, chunk_delta: timedelta, processed: int) -> int:
    if current >= end_ts:
        return processed
    chunk_seconds = max(1.0, chunk_delta.total_seconds())
    remaining_seconds = max(0.0, (end_ts - current).total_seconds())
    return processed + max(1, math.ceil(remaining_seconds / chunk_seconds))


def _raise_incomplete_event_enrichment(
    *,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    message: str,
    context: Dict[str, Any],
) -> None:
    log_fn(
        "ERROR",
        "DB upsert skipped due to incomplete event enrichment",
        {
            "phase": context.get("phase", "fetch_vehicle_events"),
            "complete_event_enrichment": False,
            **context,
        },
    )
    raise TelematicsProviderSafetyError("INCOMPLETE_EVENT_ENRICHMENT", message, context=context)


def _response_body_summary(value: Any, *, limit: int = 500) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _vehicle_events_gap_record(
    *,
    scope: str,
    registration: Optional[str],
    start_ts: datetime,
    end_ts: datetime,
    mode: str,
    exc: TelematicsProviderSafetyError,
    split_depth: int,
    min_chunk_delta: timedelta,
) -> Dict[str, Any]:
    return {
        "scope": scope,
        "registration": registration,
        "chunk_start_ts": start_ts.isoformat(),
        "chunk_end_ts": end_ts.isoformat(),
        "duration_seconds": round(_event_window_seconds(start_ts, end_ts), 3),
        "endpoint": exc.context.get("endpoint") or "/vehicles/events",
        "mode": mode,
        "failure_code": exc.code,
        "status_code": exc.context.get("status_code"),
        "response_body_summary": _response_body_summary(exc.context.get("response_body_text")),
        "attempts": exc.context.get("attempts"),
        "split_depth": split_depth,
        "min_chunk_minutes": round(min_chunk_delta.total_seconds() / 60, 3),
    }


def _record_vehicle_events_gap(
    *,
    gaps: List[Dict[str, Any]],
    gap: Dict[str, Any],
    config: BestEffortConfig,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
) -> None:
    if len(gaps) >= config.max_gaps_per_run:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Audited best-effort vehicle-events gap cap exceeded",
            context={
                "phase": "fetch_vehicle_events",
                "abort_code": "BEST_EFFORT_MAX_GAPS_EXCEEDED",
                "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
                "event_gap_count": len(gaps),
                "max_gaps_per_run": config.max_gaps_per_run,
                "complete_event_enrichment": False,
            },
        )
    gaps.append(gap)
    log_fn(
        "WARNING",
        "Audited best-effort vehicle-events gap recorded",
        {
            "phase": "fetch_vehicle_events",
            "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
            "complete_event_enrichment": False,
            "event_gap_count": len(gaps),
            **gap,
        },
    )


def _vehicle_events_gap_summary(gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
    affected = sorted({
        str(gap.get("registration")).strip()
        for gap in gaps
        if str(gap.get("registration") or "").strip()
    })
    return {
        "event_enrichment_status": "partial" if gaps else "complete",
        "complete_event_enrichment": not bool(gaps),
        "event_gap_count": len(gaps),
        "fleet_gap_count": sum(1 for gap in gaps if gap.get("scope") == "fleet"),
        "registration_gap_count": sum(1 for gap in gaps if gap.get("scope") == "registration"),
        "affected_registrations_count": len(affected),
        "affected_registrations_sample": affected[:25],
        "total_gap_duration_seconds": round(sum(float(gap.get("duration_seconds") or 0) for gap in gaps), 3),
        "speeding_rpm_counts_are_partial": bool(gaps),
    }


def _vehicle_events_disabled_summary() -> Dict[str, Any]:
    return {
        "event_enrichment_status": "disabled",
        "complete_event_enrichment": True,
        "event_gap_count": 0,
        "fleet_gap_count": 0,
        "registration_gap_count": 0,
        "affected_registrations_count": 0,
        "affected_registrations_sample": [],
        "total_gap_duration_seconds": 0,
        "speeding_rpm_counts_are_partial": False,
        "event_enrichment_disabled": True,
        "event_derived_counts_are_zero": True,
    }


def _vehicle_events_source_mismatch_summary(selected_source: str) -> Dict[str, Any]:
    return {
        "event_enrichment_status": "source_mismatch",
        "complete_event_enrichment": True,
        "event_gap_count": 0,
        "fleet_gap_count": 0,
        "registration_gap_count": 0,
        "affected_registrations_count": 0,
        "affected_registrations_sample": [],
        "total_gap_duration_seconds": 0,
        "speeding_rpm_counts_are_partial": False,
        "event_derived_counts_are_zero": False,
        "event_derived_metric_population_skipped": True,
        "trip_metrics_population_source": selected_source,
        "required_trip_metrics_population_source": TRIP_METRICS_SOURCE_API,
        "skip_reason": TRIP_METRICS_SOURCE_MISMATCH_REASON,
    }


def _write_vehicle_events_gap_audit_artifact(
    *,
    client: Any,
    run_id: str,
    client_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    mode: str,
    summary: Dict[str, Any],
    gaps: List[Dict[str, Any]],
) -> Tuple[str, Optional[str]]:
    out_dir = Path(os.getenv("LOG_PLATFORM_ARTIFACT_TMP_DIR", "/tmp"))
    safe_run_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(run_id))
    out_dir = out_dir / f"vehicle_events_gap_audit_{safe_run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "vehicle_events_gap_audit.json"
    payload = {
        "kind": "VEHICLE_EVENTS_GAP_AUDIT",
        "client_id": client_id,
        "run_id": run_id,
        "window_start_ts": window_start_ts.isoformat(),
        "window_end_ts": window_end_ts.isoformat(),
        "event_enrichment_mode": mode,
        "summary": summary,
        "gaps": gaps,
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifact_id = client.upload_artifact(str(path), kind="VEHICLE_EVENTS_GAP_AUDIT", run_id=run_id)
    return str(path), artifact_id


def _ensure_registration_fallback_budget_capacity(
    *,
    provider: TelematicsFleetProviderClient,
    config: RegistrationFallbackConfig,
    estimated_requests: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    context: Dict[str, Any],
) -> None:
    if estimated_requests > config.max_requests_per_run:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback estimated requests exceed fallback request budget",
            context={
                **context,
                "abort_code": "REGISTRATION_FALLBACK_ESTIMATED_REQUESTS_EXCEED_BUDGET",
                "estimated_requests": estimated_requests,
                "fallback_max_requests_per_run": config.max_requests_per_run,
            },
        )

    remaining_global = provider.budget.limits.max_requests_per_run - provider.budget.total_requests
    if not config.max_requests_per_run_explicit and estimated_requests > remaining_global:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback would exceed existing global provider request budget",
            context={
                **context,
                "abort_code": "REGISTRATION_FALLBACK_REQUIRES_EXPLICIT_REQUEST_BUDGET",
                "estimated_requests": estimated_requests,
                "remaining_global_provider_requests": remaining_global,
                "fallback_max_requests_explicit": False,
            },
        )

    if config.max_requests_per_run_explicit:
        provider.budget.limits.max_requests_per_run = max(
            provider.budget.limits.max_requests_per_run,
            provider.budget.total_requests + config.max_requests_per_run,
        )
        provider.budget.limits.max_requests_per_endpoint = max(
            provider.budget.limits.max_requests_per_endpoint,
            provider.budget.endpoint_requests.get("/vehicles/events:registration", 0) + config.max_requests_per_run,
        )
        provider.budget.limits.max_requests_per_subwindow = max(
            provider.budget.limits.max_requests_per_subwindow,
            config.max_requests_per_run,
        )


def _fallback_requests_used(provider: TelematicsFleetProviderClient, start_total_requests: int) -> int:
    return max(0, provider.budget.total_requests - start_total_requests)


def _ensure_registration_fallback_actual_budget(
    *,
    provider: TelematicsFleetProviderClient,
    config: RegistrationFallbackConfig,
    start_total_requests: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    context: Dict[str, Any],
) -> None:
    used = _fallback_requests_used(provider, start_total_requests)
    if used >= config.max_requests_per_run:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback request budget exhausted",
            context={
                **context,
                "abort_code": "REGISTRATION_FALLBACK_REQUEST_BUDGET_EXHAUSTED",
                "fallback_requests_used": used,
                "fallback_max_requests_per_run": config.max_requests_per_run,
            },
        )


def _clamp_events_to_window(
    events: List[Dict[str, Any]],
    *,
    start_ts: datetime,
    end_ts: datetime,
    exclusive_start: bool,
) -> List[Dict[str, Any]]:
    """Cut a fetch result down to the window that was actually asked for.

    Necessary because the request bound and the result are not the same
    interval. `vehicle_events_wire_window` deliberately *widens* the wire window
    — its docstring says over-fetching is safe, on the grounds that a stray
    event "simply matches no trip in that interval" and that duplicates are
    "dropped by the existing per-event dedupe keys". Both hold for RPM, which
    dedupes on `_rpm_vehicle_event_dedupe_key`. Neither holds for speeding:
    `_compute_speeding_violation_counts` counts every violation handed to it,
    so the same 165 km/h sample arriving from two sibling windows is two
    violations, permanently, on a newly captured trip.

    So siblings are made disjoint in *result* space, not merely in requested
    space: the right-hand side of a split is exclusive at its start, the left is
    inclusive, and both are clamped to their own bounds. That is exact whatever
    the wire does to the request — widening cannot leak an event across the
    seam, and second-precision truncation of the bound cannot open a hole,
    because the seam instant itself is whole-second (see the split below).

    An event with no timestamp is kept by the inclusive side only. It cannot be
    placed, and both matching functions skip a `None` event_ts anyway, so it can
    never reach a count — keeping it on one side preserves the record without
    letting it double. A malformed timestamp propagates out of
    `_parse_provider_dt`, exactly as it does for those two callers: this filter
    must not become the one place that tolerates provider data the job refuses.
    """
    kept: List[Dict[str, Any]] = []
    for event in events:
        event_ts = _parse_provider_dt(event.get("event_ts"))
        if event_ts is None:
            if not exclusive_start:
                kept.append(event)
            continue
        if event_ts > end_ts:
            continue
        if event_ts <= start_ts if exclusive_start else event_ts < start_ts:
            continue
        kept.append(event)
    return kept


def _fetch_vehicle_events_candidate_window(
    *,
    provider: TelematicsFleetProviderClient,
    registration: str,
    start_ts: datetime,
    end_ts: datetime,
    limit: int,
    max_pages: int,
    timeout_s: int,
    min_chunk_delta: timedelta,
    max_split_depth: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    context: Dict[str, Any],
    depth: int = 0,
    exclusive_start: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Fetch one candidate window completely, or refuse to return at all.

    This is deliberately NOT
    `_fetch_vehicle_events_registration_window_adaptive`. That helper is the
    *fallback* path, and it calls `_ensure_registration_fallback_actual_budget`,
    which raises `max_requests_per_endpoint` and `max_requests_per_subwindow` so
    a rescue attempt can outspend the configured ceiling. Rescuing a failed
    fleet scan may be worth that; routine reconciliation is not, and a primary
    path that quietly lifts the provider safety limits is the opposite of the
    budget guarantee this scope exists to provide. So this path spends inside
    the configured limits and fails when they are reached.

    Completeness is not best-effort either. Every abort — page cap, budget,
    split floor, split depth — raises. A partially fetched window would produce
    an under-count that is indistinguishable from a real zero once written, and
    for a newly captured trip that zero is permanent: nothing revisits it,
    because the next reconciliation will correctly see the row as already
    captured. Fail-closed is the only safe direction here.
    """
    sw = provider_sub_window_label(start_ts, end_ts)
    abort_context = {
        **context,
        "endpoint": "/vehicles/events:registration",
        "registration": registration,
        "chunk_start_ts": start_ts.isoformat(),
        "chunk_end_ts": end_ts.isoformat(),
        "split_depth": depth,
        "vehicle_events_scope": VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES,
        "complete_event_enrichment": False,
    }
    if depth > max_split_depth:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Reconciliation candidate window split depth exceeded",
            context={**abort_context, "abort_code": "CANDIDATE_MAX_SPLIT_DEPTH_EXCEEDED"},
        )

    events, stats = provider.fetch_vehicle_events_registration(
        registration=registration,
        start_timestamp=start_ts,
        end_timestamp=end_ts,
        sub_window_label=f"vehicle_events_candidate:{sw}",
        limit=limit,
        max_pages=max_pages,
        timeout_s=timeout_s,
        return_stats=True,
        log_context={**context, "registration": registration, "candidate_split_depth": depth},
    )
    if not stats.get("stopped_by_max_pages"):
        events = _clamp_events_to_window(
            events, start_ts=start_ts, end_ts=end_ts, exclusive_start=exclusive_start,
        )
        return events, {
            "pages_fetched": int(stats.get("pages_fetched") or 0),
            "records_fetched": len(events),
            "candidate_windows_fetched": 1,
        }

    # The page cap hit, so this window is not fully described. Halve it and
    # describe each half completely; discard the partial result rather than
    # merging it, because a page-capped page set has no completeness meaning.
    if (end_ts - start_ts) <= min_chunk_delta:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Reconciliation candidate window hit the page cap at the minimum chunk size",
            context={**abort_context, "abort_code": "CANDIDATE_MIN_CHUNK_PAGE_CAP"},
        )
    # The two halves must PARTITION the window, in results and not merely in
    # requests. Both halves are clamped to their own bounds by
    # `_clamp_events_to_window`, the right one exclusively at the seam, so
    # neither the wire window's deliberate widening nor a page boundary can put
    # one event on both sides. That matters because a duplicate is not
    # harmless here: speeding has no dedupe key, so a doubled 165 km/h sample is
    # a permanently inflated `speeding_160_170_count` on a brand-new trip.
    #
    # The seam is pinned to a whole second because that is the resolution the
    # provider is addressed in: `PROVIDER_WIRE_DT_FORMAT` is "%Y-%m-%d %H:%M:%S"
    # and `strftime` TRUNCATES. A seam at .5s would be written to both requests
    # as the same truncated second, and the right half's exclusive filter would
    # then discard events the left half was never asked for — a hole rather than
    # an overlap. On a whole second, truncation is the identity.
    mid_ts = (start_ts + (end_ts - start_ts) / 2).replace(microsecond=0)
    if not start_ts < mid_ts < end_ts:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Reconciliation candidate window is too short to split at wire resolution",
            context={**abort_context, "abort_code": "CANDIDATE_SEAM_BELOW_WIRE_RESOLUTION"},
        )
    left_events, left_stats = _fetch_vehicle_events_candidate_window(
        provider=provider, registration=registration, start_ts=start_ts, end_ts=mid_ts,
        limit=limit, max_pages=max_pages, timeout_s=timeout_s,
        min_chunk_delta=min_chunk_delta, max_split_depth=max_split_depth,
        log_fn=log_fn, context=context, depth=depth + 1,
        exclusive_start=exclusive_start,
    )
    right_events, right_stats = _fetch_vehicle_events_candidate_window(
        provider=provider, registration=registration, start_ts=mid_ts, end_ts=end_ts,
        limit=limit, max_pages=max_pages, timeout_s=timeout_s,
        min_chunk_delta=min_chunk_delta, max_split_depth=max_split_depth,
        log_fn=log_fn, context=context, depth=depth + 1,
        exclusive_start=True,
    )
    return left_events + right_events, {
        # The capped parent attempt is counted too. Its RESULT is discarded —
        # a page-capped page set has no completeness meaning — but its pages
        # were really fetched, and this field is what an operator reads to see
        # what a run cost. Dropping them would make every split under-report.
        # (The provider budget itself is unaffected either way: it is tracked by
        # `ProviderRunBudget` on the request path, not derived from this stat.)
        "pages_fetched": (
            int(stats.get("pages_fetched") or 0)
            + left_stats["pages_fetched"] + right_stats["pages_fetched"]
        ),
        "records_fetched": len(left_events) + len(right_events),  # post-seam
        "candidate_windows_fetched": (
            left_stats["candidate_windows_fetched"] + right_stats["candidate_windows_fetched"]
        ),
    }


def _fetch_vehicle_events_registration_window_adaptive(
    *,
    provider: TelematicsFleetProviderClient,
    registration: str,
    start_ts: datetime,
    end_ts: datetime,
    request_chunk_delta: timedelta,
    config: RegistrationFallbackConfig,
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    fallback_context: Dict[str, Any],
    start_total_requests: int,
    depth: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    _ensure_registration_fallback_actual_budget(
        provider=provider,
        config=config,
        start_total_requests=start_total_requests,
        log_fn=log_fn,
        context={
            **fallback_context,
            "registration": registration,
            "chunk_start_ts": start_ts.isoformat(),
            "chunk_end_ts": end_ts.isoformat(),
            "phase": "fetch_vehicle_events_registration_fallback",
        },
    )

    sw = provider_sub_window_label(start_ts, end_ts)
    try:
        events, stats = provider.fetch_vehicle_events_registration(
            registration=registration,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            sub_window_label=f"vehicle_events_registration_fallback:{sw}",
            limit=limit,
            max_pages=max_pages,
            timeout_s=timeout_s,
            return_stats=True,
            log_context={
                **fallback_context,
                "registration": registration,
                "fallback_subchunk_depth": depth,
                "fallback_chunk_start_ts": start_ts.isoformat(),
                "fallback_chunk_end_ts": end_ts.isoformat(),
            },
        )
        if stats.get("stopped_by_max_pages"):
            raise TelematicsProviderSafetyError(
                "VEHICLE_EVENTS_MAX_PAGES_REACHED",
                "Registration fallback page cap reached before the registration window completed",
                context={
                    **fallback_context,
                    "endpoint": "/vehicles/events:registration",
                    "registration": registration,
                    "sub_window": f"vehicle_events_registration_fallback:{sw}",
                    "max_pages": max_pages,
                    "pages_fetched": stats.get("pages_fetched"),
                    "records_fetched": stats.get("records_fetched"),
                },
            )
        return events, {
            "pages_fetched": int(stats.get("pages_fetched") or 0),
            "records_fetched": int(stats.get("records_fetched") or 0),
            "subchunks_fetched": 1 if depth > 0 else 0,
        }
    except TelematicsProviderSafetyError as exc:
        exc.context.setdefault("registration", registration)
        exc.context.setdefault("chunk_start_ts", start_ts.isoformat())
        exc.context.setdefault("chunk_end_ts", end_ts.isoformat())
        duration_s = _event_window_seconds(start_ts, end_ts)
        can_subdivide = (
            _vehicle_events_error_can_fallback(exc)
            and duration_s > config.min_chunk_delta.total_seconds()
            and request_chunk_delta > config.min_chunk_delta
        )
        if not can_subdivide:
            log_fn(
                "ERROR",
                "Registration fallback failed at minimum subchunk; DB upsert will be skipped",
                {
                    **fallback_context,
                    **exc.context,
                    "phase": "fetch_vehicle_events_registration_fallback",
                    "abort_code": exc.code,
                    "registration": registration,
                    "chunk_start_ts": start_ts.isoformat(),
                    "chunk_end_ts": end_ts.isoformat(),
                    "fallback_subchunk_depth": depth,
                    "fallback_min_chunk_minutes": round(config.min_chunk_delta.total_seconds() / 60, 3),
                    "complete_event_enrichment": False,
                },
            )
            raise

        next_delta = _halve_timedelta(request_chunk_delta, minimum=config.min_chunk_delta)
        log_fn(
            "WARNING",
            "Registration fallback window failed; subdividing registration window",
            {
                **fallback_context,
                **exc.context,
                "phase": "fetch_vehicle_events_registration_fallback",
                "abort_code": exc.code,
                "registration": registration,
                "chunk_start_ts": start_ts.isoformat(),
                "chunk_end_ts": end_ts.isoformat(),
                "old_subchunk_minutes": round(request_chunk_delta.total_seconds() / 60, 3),
                "new_subchunk_minutes": round(next_delta.total_seconds() / 60, 3),
                "fallback_subchunk_depth": depth + 1,
            },
        )

        all_events: List[Dict[str, Any]] = []
        total_pages = 0
        total_records = 0
        total_subchunks = 0
        subwindows = list(_iter_inclusive_event_subwindows(start_ts, end_ts, chunk_delta=next_delta))
        for sub_index, (sub_start, sub_end) in enumerate(subwindows, start=1):
            log_fn(
                "INFO",
                "Registration fallback subchunk progress",
                {
                    **fallback_context,
                    "phase": "fetch_vehicle_events_registration_fallback",
                    "registration": registration,
                    "subchunk_index": sub_index,
                    "subchunk_total": len(subwindows),
                    "subchunk_start_ts": sub_start.isoformat(),
                    "subchunk_end_ts": sub_end.isoformat(),
                    "subchunk_minutes": round(_event_window_seconds(sub_start, sub_end) / 60, 3),
                },
            )
            sub_events, sub_stats = _fetch_vehicle_events_registration_window_adaptive(
                provider=provider,
                registration=registration,
                start_ts=sub_start,
                end_ts=sub_end,
                request_chunk_delta=next_delta,
                config=config,
                limit=limit,
                max_pages=max_pages,
                timeout_s=timeout_s,
                log_fn=log_fn,
                fallback_context=fallback_context,
                start_total_requests=start_total_requests,
                depth=depth + 1,
            )
            all_events.extend(sub_events)
            total_pages += sub_stats["pages_fetched"]
            total_records += sub_stats["records_fetched"]
            total_subchunks += max(1, sub_stats["subchunks_fetched"])

        return all_events, {
            "pages_fetched": total_pages,
            "records_fetched": total_records,
            "subchunks_fetched": total_subchunks,
        }


def _fetch_vehicle_events_registration_fallback_chunk(
    *,
    provider: TelematicsFleetProviderClient,
    start_ts: datetime,
    end_ts: datetime,
    registrations: List[str],
    config: RegistrationFallbackConfig,
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    failed_fleet_error: TelematicsProviderSafetyError,
    fallback_state: Dict[str, int],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    sw = provider_sub_window_label(start_ts, end_ts)
    context = {
        "endpoint": "/vehicles/events",
        "phase": "fetch_vehicle_events_registration_fallback",
        "sub_window": f"vehicle_events_fleet:{sw}",
        "chunk_start_ts": start_ts.isoformat(),
        "chunk_end_ts": end_ts.isoformat(),
        "fleet_abort_code": failed_fleet_error.code,
        "fleet_status_code": failed_fleet_error.context.get("status_code"),
    }

    if not config.enabled:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback disabled after terminal fleet vehicle-events failure",
            context={
                **context,
                **failed_fleet_error.context,
                "abort_code": "REGISTRATION_FALLBACK_DISABLED",
            },
        )
    if not registrations:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback has no registrations to query",
            context={**context, "abort_code": "REGISTRATION_FALLBACK_NO_REGISTRATIONS"},
        )
    if len(registrations) > config.max_registrations:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback registration count exceeds configured cap",
            context={
                **context,
                "abort_code": "REGISTRATION_FALLBACK_MAX_REGISTRATIONS",
                "registrations_count": len(registrations),
                "fallback_max_registrations": config.max_registrations,
            },
        )
    if fallback_state.get("chunks_started", 0) >= config.max_chunks:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Registration fallback chunk count exceeds configured cap",
            context={
                **context,
                "abort_code": "REGISTRATION_FALLBACK_MAX_CHUNKS",
                "fallback_chunks_started": fallback_state.get("chunks_started", 0),
                "fallback_max_chunks": config.max_chunks,
            },
        )

    estimated_requests = len(registrations)
    estimated_min_duration_seconds = estimated_requests / config.rps
    _ensure_registration_fallback_budget_capacity(
        provider=provider,
        config=config,
        estimated_requests=estimated_requests,
        log_fn=log_fn,
        context={**context, "registrations_count": len(registrations)},
    )

    fallback_state["chunks_started"] = fallback_state.get("chunks_started", 0) + 1
    start_total_requests = provider.budget.total_requests
    old_rps = provider.rate_limit_rps
    provider.rate_limit_rps = config.rps
    started_at = time.monotonic()

    log_fn(
        "INFO",
        "Registration fallback start for failed fleet vehicle-events chunk",
        {
            **context,
            "registrations_count": len(registrations),
            "fallback_rps": config.rps,
            "estimated_requests": estimated_requests,
            "estimated_min_duration_seconds": round(estimated_min_duration_seconds, 3),
            "fallback_max_requests_per_run": config.max_requests_per_run,
            "fallback_max_requests_explicit": config.max_requests_per_run_explicit,
            "fallback_min_chunk_minutes": round(config.min_chunk_delta.total_seconds() / 60, 3),
            "complete_event_enrichment": False,
        },
    )

    all_events: List[Dict[str, Any]] = []
    successful = 0
    failed_so_far = 0
    pages_fetched = 0
    subchunks_fetched = 0
    try:
        for index, registration in enumerate(registrations, start=1):
            events, stats = _fetch_vehicle_events_registration_window_adaptive(
                provider=provider,
                registration=registration,
                start_ts=start_ts,
                end_ts=end_ts,
                request_chunk_delta=timedelta(seconds=_event_window_seconds(start_ts, end_ts)),
                config=config,
                limit=limit,
                max_pages=max_pages,
                timeout_s=timeout_s,
                log_fn=log_fn,
                fallback_context=context,
                start_total_requests=start_total_requests,
            )
            successful += 1
            all_events.extend(events)
            pages_fetched += stats["pages_fetched"]
            subchunks_fetched += stats["subchunks_fetched"]

            if (
                index == 1
                or index == len(registrations)
                or index % max(1, config.progress_interval) == 0
            ):
                elapsed_s = time.monotonic() - started_at
                avg_s = elapsed_s / index if index else 0
                remaining_s = max(0.0, avg_s * (len(registrations) - index))
                log_fn(
                    "INFO",
                    "Registration fallback progress",
                    {
                        **context,
                        "completed": index,
                        "total": len(registrations),
                        "percent": round(index * 100.0 / len(registrations), 3),
                        "successful": successful,
                        "failed_so_far": failed_so_far,
                        "elapsed_seconds": round(elapsed_s, 3),
                        "estimated_remaining_seconds": round(remaining_s, 3),
                        "current_registration": registration,
                        "fallback_requests": _fallback_requests_used(provider, start_total_requests),
                        "fallback_events_fetched": len(all_events),
                    },
                )
    except TelematicsProviderSafetyError as exc:
        failed_so_far += 1
        log_fn(
            "ERROR",
            "Registration fallback failed; DB upsert skipped due to incomplete event enrichment",
            {
                **context,
                **exc.context,
                "phase": "fetch_vehicle_events_registration_fallback",
                "abort_code": exc.code,
                "failed_registration": exc.context.get("registration"),
                "fallback_requests": _fallback_requests_used(provider, start_total_requests),
                "fallback_events_fetched": len(all_events),
                "complete_event_enrichment": False,
            },
        )
        raise
    finally:
        provider.rate_limit_rps = old_rps

    fallback_requests = _fallback_requests_used(provider, start_total_requests)
    fallback_state["chunks_successful"] = fallback_state.get("chunks_successful", 0) + 1
    fallback_state["requests"] = fallback_state.get("requests", 0) + fallback_requests
    fallback_state["events_fetched"] = fallback_state.get("events_fetched", 0) + len(all_events)
    fallback_state["subchunks_fetched"] = fallback_state.get("subchunks_fetched", 0) + subchunks_fetched

    stats = {
        **context,
        "source": "registration_fallback",
        "registrations_count": len(registrations),
        "successful_registrations": successful,
        "failed_registrations": failed_so_far,
        "pages_fetched": pages_fetched,
        "records_fetched": len(all_events),
        "fallback_requests": fallback_requests,
        "fallback_subchunks_fetched": subchunks_fetched,
        "complete_event_enrichment": True,
    }
    log_fn("INFO", "Registration fallback completed for failed fleet vehicle-events chunk", stats)
    return all_events, stats


def _raise_best_effort_fail_fast(
    *,
    exc: TelematicsProviderSafetyError,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    context: Dict[str, Any],
) -> None:
    _raise_incomplete_event_enrichment(
        log_fn=log_fn,
        message="Audited best-effort vehicle-events fetch hit a fail-fast safety condition",
        context={
            **context,
            **exc.context,
            "abort_code": exc.code,
            "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
            "complete_event_enrichment": False,
        },
    )


def _fetch_vehicle_events_registration_window_best_effort(
    *,
    provider: TelematicsFleetProviderClient,
    registration: str,
    start_ts: datetime,
    end_ts: datetime,
    request_chunk_delta: timedelta,
    config: RegistrationFallbackConfig,
    best_effort_config: BestEffortConfig,
    gaps: List[Dict[str, Any]],
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    context: Dict[str, Any],
    start_total_requests: int,
    depth: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    if depth > best_effort_config.max_split_depth:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Audited best-effort registration split depth exceeded",
            context={
                **context,
                "registration": registration,
                "chunk_start_ts": start_ts.isoformat(),
                "chunk_end_ts": end_ts.isoformat(),
                "abort_code": "BEST_EFFORT_MAX_SPLIT_DEPTH_EXCEEDED",
                "split_depth": depth,
                "max_split_depth": best_effort_config.max_split_depth,
                "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
                "complete_event_enrichment": False,
            },
        )

    _ensure_registration_fallback_actual_budget(
        provider=provider,
        config=config,
        start_total_requests=start_total_requests,
        log_fn=log_fn,
        context={
            **context,
            "registration": registration,
            "chunk_start_ts": start_ts.isoformat(),
            "chunk_end_ts": end_ts.isoformat(),
            "phase": "fetch_vehicle_events_registration_recovery",
        },
    )

    sw = provider_sub_window_label(start_ts, end_ts)
    try:
        events, stats = provider.fetch_vehicle_events_registration(
            registration=registration,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            sub_window_label=f"vehicle_events_registration_best_effort:{sw}",
            limit=limit,
            max_pages=max_pages,
            timeout_s=timeout_s,
            return_stats=True,
            log_context={
                **context,
                "registration": registration,
                "best_effort_split_depth": depth,
                "best_effort_chunk_start_ts": start_ts.isoformat(),
                "best_effort_chunk_end_ts": end_ts.isoformat(),
            },
        )
        if stats.get("stopped_by_max_pages"):
            raise TelematicsProviderSafetyError(
                "VEHICLE_EVENTS_MAX_PAGES_REACHED",
                "Registration best-effort page cap reached before the registration window completed",
                context={
                    **context,
                    "endpoint": "/vehicles/events:registration",
                    "registration": registration,
                    "sub_window": f"vehicle_events_registration_best_effort:{sw}",
                    "max_pages": max_pages,
                    "pages_fetched": stats.get("pages_fetched"),
                    "records_fetched": stats.get("records_fetched"),
                },
            )
        return events, {
            "pages_fetched": int(stats.get("pages_fetched") or 0),
            "records_fetched": int(stats.get("records_fetched") or 0),
            "registration_subchunks_successful": 1,
        }
    except TelematicsProviderSafetyError as exc:
        exc.context.setdefault("registration", registration)
        exc.context.setdefault("chunk_start_ts", start_ts.isoformat())
        exc.context.setdefault("chunk_end_ts", end_ts.isoformat())
        if _vehicle_events_error_is_best_effort_fail_fast(exc):
            _raise_best_effort_fail_fast(
                exc=exc,
                log_fn=log_fn,
                context={**context, "phase": "fetch_vehicle_events_registration_recovery"},
            )
        if not _vehicle_events_error_is_best_effort_gap(exc):
            _raise_best_effort_fail_fast(
                exc=exc,
                log_fn=log_fn,
                context={**context, "phase": "fetch_vehicle_events_registration_recovery"},
            )

        duration_s = _event_window_seconds(start_ts, end_ts)
        can_subdivide = (
            duration_s > best_effort_config.min_registration_chunk_delta.total_seconds()
            and request_chunk_delta > best_effort_config.min_registration_chunk_delta
        )
        if can_subdivide:
            next_delta = _halve_timedelta(
                request_chunk_delta,
                minimum=best_effort_config.min_registration_chunk_delta,
            )
            log_fn(
                "WARNING",
                "Audited best-effort registration window failed; subdividing",
                {
                    **context,
                    **exc.context,
                    "phase": "fetch_vehicle_events_registration_recovery",
                    "abort_code": exc.code,
                    "registration": registration,
                    "chunk_start_ts": start_ts.isoformat(),
                    "chunk_end_ts": end_ts.isoformat(),
                    "old_subchunk_minutes": round(request_chunk_delta.total_seconds() / 60, 3),
                    "new_subchunk_minutes": round(next_delta.total_seconds() / 60, 3),
                    "split_depth": depth + 1,
                    "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
                },
            )
            all_events: List[Dict[str, Any]] = []
            pages_fetched = 0
            records_fetched = 0
            subchunks_successful = 0
            subwindows = list(_iter_inclusive_event_subwindows(start_ts, end_ts, chunk_delta=next_delta))
            for sub_index, (sub_start, sub_end) in enumerate(subwindows, start=1):
                log_fn(
                    "INFO",
                    "Audited best-effort registration subchunk progress",
                    {
                        **context,
                        "phase": "fetch_vehicle_events_registration_recovery",
                        "registration": registration,
                        "subchunk_index": sub_index,
                        "subchunk_total": len(subwindows),
                        "subchunk_start_ts": sub_start.isoformat(),
                        "subchunk_end_ts": sub_end.isoformat(),
                        "subchunk_minutes": round(_event_window_seconds(sub_start, sub_end) / 60, 3),
                        "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
                    },
                )
                sub_events, sub_stats = _fetch_vehicle_events_registration_window_best_effort(
                    provider=provider,
                    registration=registration,
                    start_ts=sub_start,
                    end_ts=sub_end,
                    request_chunk_delta=next_delta,
                    config=config,
                    best_effort_config=best_effort_config,
                    gaps=gaps,
                    limit=limit,
                    max_pages=max_pages,
                    timeout_s=timeout_s,
                    log_fn=log_fn,
                    context=context,
                    start_total_requests=start_total_requests,
                    depth=depth + 1,
                )
                all_events.extend(sub_events)
                pages_fetched += sub_stats["pages_fetched"]
                records_fetched += sub_stats["records_fetched"]
                subchunks_successful += sub_stats["registration_subchunks_successful"]
            return all_events, {
                "pages_fetched": pages_fetched,
                "records_fetched": records_fetched,
                "registration_subchunks_successful": subchunks_successful,
            }

        gap = _vehicle_events_gap_record(
            scope="registration",
            registration=registration,
            start_ts=start_ts,
            end_ts=end_ts,
            mode=VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
            exc=exc,
            split_depth=depth,
            min_chunk_delta=best_effort_config.min_registration_chunk_delta,
        )
        _record_vehicle_events_gap(
            gaps=gaps,
            gap=gap,
            config=best_effort_config,
            log_fn=log_fn,
        )
        return [], {
            "pages_fetched": 0,
            "records_fetched": 0,
            "registration_subchunks_successful": 0,
        }


def _fetch_vehicle_events_registration_recovery_best_effort(
    *,
    provider: TelematicsFleetProviderClient,
    start_ts: datetime,
    end_ts: datetime,
    registrations: List[str],
    fallback_config: RegistrationFallbackConfig,
    best_effort_config: BestEffortConfig,
    gaps: List[Dict[str, Any]],
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    failed_fleet_error: TelematicsProviderSafetyError,
    fallback_state: Dict[str, int],
) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    sw = provider_sub_window_label(start_ts, end_ts)
    context = {
        "endpoint": "/vehicles/events",
        "phase": "fetch_vehicle_events_registration_recovery",
        "sub_window": f"vehicle_events_fleet:{sw}",
        "chunk_start_ts": start_ts.isoformat(),
        "chunk_end_ts": end_ts.isoformat(),
        "fleet_abort_code": failed_fleet_error.code,
        "fleet_status_code": failed_fleet_error.context.get("status_code"),
        "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
    }

    if not fallback_config.enabled:
        log_fn("INFO", "Audited best-effort registration recovery skipped; fallback disabled", context)
        return None, {"recovery_skipped": "fallback_disabled"}
    if not registrations:
        log_fn("WARNING", "Audited best-effort registration recovery skipped; no registrations", context)
        return None, {"recovery_skipped": "no_registrations"}
    if len(registrations) > fallback_config.max_registrations:
        log_fn(
            "WARNING",
            "Audited best-effort registration recovery skipped; registration cap exceeded",
            {
                **context,
                "registrations_count": len(registrations),
                "fallback_max_registrations": fallback_config.max_registrations,
            },
        )
        return None, {"recovery_skipped": "max_registrations"}
    if fallback_state.get("chunks_started", 0) >= fallback_config.max_chunks:
        log_fn(
            "WARNING",
            "Audited best-effort registration recovery skipped; fallback chunk cap reached",
            {
                **context,
                "fallback_chunks_started": fallback_state.get("chunks_started", 0),
                "fallback_max_chunks": fallback_config.max_chunks,
            },
        )
        return None, {"recovery_skipped": "max_chunks"}

    estimated_requests = len(registrations)
    _ensure_registration_fallback_budget_capacity(
        provider=provider,
        config=fallback_config,
        estimated_requests=estimated_requests,
        log_fn=log_fn,
        context={**context, "registrations_count": len(registrations)},
    )

    fallback_state["chunks_started"] = fallback_state.get("chunks_started", 0) + 1
    start_total_requests = provider.budget.total_requests
    old_rps = provider.rate_limit_rps
    provider.rate_limit_rps = fallback_config.rps
    started_at = time.monotonic()

    log_fn(
        "INFO",
        "Audited best-effort registration recovery start",
        {
            **context,
            "registrations_count": len(registrations),
            "fallback_rps": fallback_config.rps,
            "estimated_requests": estimated_requests,
            "estimated_min_duration_seconds": round(estimated_requests / fallback_config.rps, 3),
            "fallback_max_requests_per_run": fallback_config.max_requests_per_run,
            "fallback_min_chunk_minutes": round(
                best_effort_config.min_registration_chunk_delta.total_seconds() / 60,
                3,
            ),
        },
    )

    all_events: List[Dict[str, Any]] = []
    pages_fetched = 0
    subchunks_successful = 0
    starting_gap_count = len(gaps)
    try:
        for index, registration in enumerate(registrations, start=1):
            events, stats = _fetch_vehicle_events_registration_window_best_effort(
                provider=provider,
                registration=registration,
                start_ts=start_ts,
                end_ts=end_ts,
                request_chunk_delta=timedelta(seconds=_event_window_seconds(start_ts, end_ts)),
                config=fallback_config,
                best_effort_config=best_effort_config,
                gaps=gaps,
                limit=limit,
                max_pages=max_pages,
                timeout_s=timeout_s,
                log_fn=log_fn,
                context=context,
                start_total_requests=start_total_requests,
            )
            all_events.extend(events)
            pages_fetched += stats["pages_fetched"]
            subchunks_successful += stats["registration_subchunks_successful"]

            if (
                index == 1
                or index == len(registrations)
                or index % max(1, fallback_config.progress_interval) == 0
            ):
                elapsed_s = time.monotonic() - started_at
                avg_s = elapsed_s / index if index else 0
                remaining_s = max(0.0, avg_s * (len(registrations) - index))
                log_fn(
                    "INFO",
                    "Audited best-effort registration recovery progress",
                    {
                        **context,
                        "completed": index,
                        "total": len(registrations),
                        "percent": round(index * 100.0 / len(registrations), 3),
                        "elapsed_seconds": round(elapsed_s, 3),
                        "estimated_remaining_seconds": round(remaining_s, 3),
                        "current_registration": registration,
                        "fallback_requests": _fallback_requests_used(provider, start_total_requests),
                        "fallback_events_fetched": len(all_events),
                        "event_gap_count": len(gaps),
                    },
                )
    finally:
        provider.rate_limit_rps = old_rps

    fallback_requests = _fallback_requests_used(provider, start_total_requests)
    fallback_state["chunks_successful"] = fallback_state.get("chunks_successful", 0) + 1
    fallback_state["requests"] = fallback_state.get("requests", 0) + fallback_requests
    fallback_state["events_fetched"] = fallback_state.get("events_fetched", 0) + len(all_events)
    fallback_state["subchunks_fetched"] = fallback_state.get("subchunks_fetched", 0) + subchunks_successful

    stats = {
        **context,
        "source": "registration_fallback",
        "registrations_count": len(registrations),
        "pages_fetched": pages_fetched,
        "records_fetched": len(all_events),
        "fallback_requests": fallback_requests,
        "fallback_subchunks_fetched": subchunks_successful,
        "registration_gaps_recorded": len(gaps) - starting_gap_count,
        "complete_event_enrichment": len(gaps) == 0,
    }
    log_fn("INFO", "Audited best-effort registration recovery complete", stats)
    return all_events, stats


def _fetch_vehicle_events_fleet_window_best_effort(
    *,
    provider: TelematicsFleetProviderClient,
    start_ts: datetime,
    end_ts: datetime,
    request_chunk_delta: timedelta,
    min_fleet_chunk_delta: timedelta,
    fallback_config: RegistrationFallbackConfig,
    fallback_registrations: List[str],
    best_effort_config: BestEffortConfig,
    gaps: List[Dict[str, Any]],
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
    fallback_state: Dict[str, int],
    base_context: Dict[str, Any],
    depth: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if depth > best_effort_config.max_split_depth:
        _raise_incomplete_event_enrichment(
            log_fn=log_fn,
            message="Audited best-effort fleet split depth exceeded",
            context={
                **base_context,
                "chunk_start_ts": start_ts.isoformat(),
                "chunk_end_ts": end_ts.isoformat(),
                "abort_code": "BEST_EFFORT_MAX_SPLIT_DEPTH_EXCEEDED",
                "split_depth": depth,
                "max_split_depth": best_effort_config.max_split_depth,
                "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
                "complete_event_enrichment": False,
            },
        )

    sw = provider_sub_window_label(start_ts, end_ts)
    context = {
        **base_context,
        "endpoint": "/vehicles/events",
        "phase": "fetch_vehicle_events",
        "sub_window": f"vehicle_events_fleet_best_effort:{sw}",
        "chunk_start_ts": start_ts.isoformat(),
        "chunk_end_ts": end_ts.isoformat(),
        "split_depth": depth,
        "event_enrichment_mode": VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
    }
    try:
        events, stats = provider.fetch_vehicle_events_fleet(
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            sub_window_label=f"vehicle_events_fleet_best_effort:{sw}",
            limit=limit,
            max_pages=max_pages,
            log_context=context,
            timeout_s=timeout_s,
            return_stats=True,
        )
        if stats.get("stopped_by_max_pages"):
            raise TelematicsProviderSafetyError(
                "VEHICLE_EVENTS_MAX_PAGES_REACHED",
                "Fleet vehicle-events page cap reached before chunk completed",
                context={
                    **context,
                    "pages_fetched": stats.get("pages_fetched"),
                    "records_fetched": stats.get("records_fetched"),
                    "stopped_by_max_pages": True,
                },
            )
        return events, {
            **context,
            "source": "fleet",
            "status": "success",
            "pages_fetched": int(stats.get("pages_fetched") or 0),
            "records_fetched": int(stats.get("records_fetched") or 0),
            "fleet_subchunks_successful": 1,
            "complete_event_enrichment": len(gaps) == 0,
        }
    except TelematicsProviderSafetyError as exc:
        exc.context.setdefault("chunk_start_ts", start_ts.isoformat())
        exc.context.setdefault("chunk_end_ts", end_ts.isoformat())
        if _vehicle_events_error_is_best_effort_fail_fast(exc):
            _raise_best_effort_fail_fast(exc=exc, log_fn=log_fn, context=context)
        if not _vehicle_events_error_is_best_effort_gap(exc):
            _raise_best_effort_fail_fast(exc=exc, log_fn=log_fn, context=context)

        duration_s = _event_window_seconds(start_ts, end_ts)
        can_subdivide = (
            duration_s > min_fleet_chunk_delta.total_seconds()
            and request_chunk_delta > min_fleet_chunk_delta
        )
        if can_subdivide:
            next_delta = _halve_timedelta(request_chunk_delta, minimum=min_fleet_chunk_delta)
            log_fn(
                "WARNING",
                "Audited best-effort fleet vehicle-events chunk failed; subdividing",
                {
                    **context,
                    **exc.context,
                    "abort_code": exc.code,
                    "old_chunk_minutes": round(request_chunk_delta.total_seconds() / 60, 3),
                    "new_chunk_minutes": round(next_delta.total_seconds() / 60, 3),
                    "split_depth": depth + 1,
                },
            )
            all_events: List[Dict[str, Any]] = []
            pages_fetched = 0
            records_fetched = 0
            fleet_subchunks_successful = 0
            registration_recovery_requests = 0
            registration_recovery_events = 0
            registration_recovery_subchunks = 0
            subwindows = list(_iter_inclusive_event_subwindows(start_ts, end_ts, chunk_delta=next_delta))
            for sub_index, (sub_start, sub_end) in enumerate(subwindows, start=1):
                log_fn(
                    "INFO",
                    "Audited best-effort fleet subchunk progress",
                    {
                        **context,
                        "subchunk_index": sub_index,
                        "subchunk_total": len(subwindows),
                        "subchunk_start_ts": sub_start.isoformat(),
                        "subchunk_end_ts": sub_end.isoformat(),
                        "subchunk_minutes": round(_event_window_seconds(sub_start, sub_end) / 60, 3),
                    },
                )
                sub_events, sub_stats = _fetch_vehicle_events_fleet_window_best_effort(
                    provider=provider,
                    start_ts=sub_start,
                    end_ts=sub_end,
                    request_chunk_delta=next_delta,
                    min_fleet_chunk_delta=min_fleet_chunk_delta,
                    fallback_config=fallback_config,
                    fallback_registrations=fallback_registrations,
                    best_effort_config=best_effort_config,
                    gaps=gaps,
                    limit=limit,
                    max_pages=max_pages,
                    timeout_s=timeout_s,
                    log_fn=log_fn,
                    fallback_state=fallback_state,
                    base_context=base_context,
                    depth=depth + 1,
                )
                all_events.extend(sub_events)
                pages_fetched += int(sub_stats.get("pages_fetched") or 0)
                records_fetched += int(sub_stats.get("records_fetched") or 0)
                fleet_subchunks_successful += int(sub_stats.get("fleet_subchunks_successful") or 0)
                registration_recovery_requests += int(sub_stats.get("fallback_requests") or 0)
                if sub_stats.get("source") == "registration_fallback":
                    registration_recovery_events += int(sub_stats.get("records_fetched") or 0)
                registration_recovery_subchunks += int(sub_stats.get("fallback_subchunks_fetched") or 0)
            return all_events, {
                **context,
                "source": "mixed_best_effort",
                "status": "partial" if gaps else "success",
                "pages_fetched": pages_fetched,
                "records_fetched": records_fetched,
                "fleet_subchunks_successful": fleet_subchunks_successful,
                "fallback_requests": registration_recovery_requests,
                "fallback_events_fetched": registration_recovery_events,
                "fallback_subchunks_fetched": registration_recovery_subchunks,
                "complete_event_enrichment": len(gaps) == 0,
            }

        recovery_events, recovery_stats = _fetch_vehicle_events_registration_recovery_best_effort(
            provider=provider,
            start_ts=start_ts,
            end_ts=end_ts,
            registrations=fallback_registrations,
            fallback_config=fallback_config,
            best_effort_config=best_effort_config,
            gaps=gaps,
            limit=limit,
            max_pages=max_pages,
            timeout_s=timeout_s,
            log_fn=log_fn,
            failed_fleet_error=exc,
            fallback_state=fallback_state,
        )
        if recovery_events is not None:
            return recovery_events, {
                **context,
                **recovery_stats,
                "fleet_subchunks_successful": 0,
                "complete_event_enrichment": len(gaps) == 0,
            }

        gap = _vehicle_events_gap_record(
            scope="fleet",
            registration=None,
            start_ts=start_ts,
            end_ts=end_ts,
            mode=VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT,
            exc=exc,
            split_depth=depth,
            min_chunk_delta=min_fleet_chunk_delta,
        )
        _record_vehicle_events_gap(
            gaps=gaps,
            gap=gap,
            config=best_effort_config,
            log_fn=log_fn,
        )
        return [], {
            **context,
            "source": "fleet_gap",
            "status": "gap_recorded",
            "pages_fetched": 0,
            "records_fetched": 0,
            "fleet_subchunks_successful": 0,
            "complete_event_enrichment": False,
        }


def _iter_fetch_vehicle_events_fleet_best_effort(
    *,
    provider: TelematicsFleetProviderClient,
    window_start_ts: datetime,
    window_end_ts: datetime,
    initial_chunk_delta: timedelta,
    min_chunk_delta: timedelta,
    fallback_config: RegistrationFallbackConfig,
    fallback_registrations: List[str],
    best_effort_config: BestEffortConfig,
    gaps: List[Dict[str, Any]],
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
) -> Iterable[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    start_ts = window_start_ts.astimezone(timezone.utc)
    end_ts = window_end_ts.astimezone(timezone.utc)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")
    if initial_chunk_delta <= timedelta(0):
        raise ValueError("initial_chunk_delta must be > 0")
    if min_chunk_delta <= timedelta(0):
        raise ValueError("min_chunk_delta must be > 0")

    current_chunk_delta = max(initial_chunk_delta, min_chunk_delta)
    min_fleet_chunk_delta = min_chunk_delta
    if best_effort_config.min_fleet_chunk_delta < min_fleet_chunk_delta:
        min_fleet_chunk_delta = best_effort_config.min_fleet_chunk_delta

    current = start_ts
    chunks_processed = 0
    fleet_chunks_successful = 0
    total_events_fetched = 0
    total_pages_fetched = 0
    fallback_state: Dict[str, int] = {
        "chunks_started": 0,
        "chunks_successful": 0,
        "requests": 0,
        "events_fetched": 0,
        "subchunks_fetched": 0,
    }

    while current < end_ts:
        chunk_end = min(current + current_chunk_delta, end_ts)
        request_end = chunk_end if chunk_end >= end_ts else chunk_end - timedelta(seconds=1)
        if request_end <= current:
            current = chunk_end
            continue

        chunk_index = chunks_processed + 1
        chunk_context = {
            "chunk_index": chunk_index,
            "chunk_total_estimated": _estimated_chunk_total(current, end_ts, current_chunk_delta, chunks_processed),
            "chunk_exclusive_end_ts": chunk_end.isoformat(),
            "chunk_duration_minutes": round((request_end - current).total_seconds() / 60, 3),
            "configured_chunk_minutes": round(current_chunk_delta.total_seconds() / 60, 3),
            "min_chunk_minutes": round(min_fleet_chunk_delta.total_seconds() / 60, 3),
            "limit": limit,
            "max_pages": max_pages,
            "timeout_s": timeout_s,
            "total_chunks_processed": chunks_processed,
            "total_events_fetched": total_events_fetched,
        }
        events, stats = _fetch_vehicle_events_fleet_window_best_effort(
            provider=provider,
            start_ts=current,
            end_ts=request_end,
            request_chunk_delta=current_chunk_delta,
            min_fleet_chunk_delta=min_fleet_chunk_delta,
            fallback_config=fallback_config,
            fallback_registrations=fallback_registrations,
            best_effort_config=best_effort_config,
            gaps=gaps,
            limit=limit,
            max_pages=max_pages,
            timeout_s=timeout_s,
            log_fn=log_fn,
            fallback_state=fallback_state,
            base_context=chunk_context,
        )

        chunks_processed += 1
        if stats.get("source") == "fleet":
            fleet_chunks_successful += 1
        else:
            fleet_chunks_successful += int(stats.get("fleet_subchunks_successful") or 0)
        total_events_fetched += len(events)
        total_pages_fetched += int(stats.get("pages_fetched") or 0)
        chunk_stats = {
            **stats,
            "total_chunks_processed": chunks_processed,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_state.get("chunks_successful", 0),
            "fallback_chunks_started": fallback_state.get("chunks_started", 0),
            "fallback_requests": int(stats.get("fallback_requests") or 0),
            "fallback_events_fetched": int(stats.get("fallback_events_fetched") or 0),
            "fallback_subchunks_fetched": int(stats.get("fallback_subchunks_fetched") or 0),
            "total_pages_fetched": total_pages_fetched,
            "total_events_fetched": total_events_fetched,
            **_vehicle_events_gap_summary(gaps),
        }
        log_fn("INFO", "Audited best-effort vehicle-events chunk processed", chunk_stats)
        yield events, chunk_stats
        current = chunk_end

    log_fn(
        "INFO",
        "Audited best-effort vehicle-events fetch complete",
        {
            "endpoint": "/vehicles/events",
            "window_start_ts": start_ts.isoformat(),
            "window_end_ts": end_ts.isoformat(),
            "total_chunks_processed": chunks_processed,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_state.get("chunks_successful", 0),
            "fallback_chunks_started": fallback_state.get("chunks_started", 0),
            "fallback_requests": fallback_state.get("requests", 0),
            "fallback_events_fetched": fallback_state.get("events_fetched", 0),
            "fallback_subchunks_fetched": fallback_state.get("subchunks_fetched", 0),
            "total_pages_fetched": total_pages_fetched,
            "total_events_fetched": total_events_fetched,
            "final_chunk_minutes": round(current_chunk_delta.total_seconds() / 60, 3),
            **_vehicle_events_gap_summary(gaps),
        },
    )


def _iter_fetch_vehicle_events_fleet_adaptive(
    *,
    provider: TelematicsFleetProviderClient,
    window_start_ts: datetime,
    window_end_ts: datetime,
    initial_chunk_delta: timedelta,
    min_chunk_delta: timedelta,
    fallback_config: RegistrationFallbackConfig,
    fallback_registrations: List[str],
    limit: int,
    max_pages: int,
    timeout_s: int,
    log_fn: Callable[[str, str, Dict[str, Any]], None],
) -> Iterable[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """Fetch fleet-wide vehicle events with bounded adaptive time chunks.

    A failed chunk is retried only after halving the remaining chunk size down
    to `min_chunk_delta`. Provider-level HTTP retries/backoff have already
    happened by the time a `TelematicsProviderSafetyError` reaches this helper.
    """
    start_ts = window_start_ts.astimezone(timezone.utc)
    end_ts = window_end_ts.astimezone(timezone.utc)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")
    if initial_chunk_delta <= timedelta(0):
        raise ValueError("initial_chunk_delta must be > 0")
    if min_chunk_delta <= timedelta(0):
        raise ValueError("min_chunk_delta must be > 0")

    current_chunk_delta = max(initial_chunk_delta, min_chunk_delta)
    current = start_ts
    chunks_processed = 0
    fleet_chunks_successful = 0
    chunks_reduced = 0
    total_events_fetched = 0
    total_pages_fetched = 0
    fallback_state: Dict[str, int] = {
        "chunks_started": 0,
        "chunks_successful": 0,
        "requests": 0,
        "events_fetched": 0,
        "subchunks_fetched": 0,
    }

    while current < end_ts:
        chunk_end = min(current + current_chunk_delta, end_ts)
        request_end = chunk_end if chunk_end >= end_ts else chunk_end - timedelta(seconds=1)
        if request_end <= current:
            current = chunk_end
            continue

        sw = provider_sub_window_label(current, request_end)
        chunk_index = chunks_processed + 1
        chunk_total_estimated = _estimated_chunk_total(current, end_ts, current_chunk_delta, chunks_processed)
        chunk_context = {
            "endpoint": "/vehicles/events",
            "phase": "fetch_vehicle_events",
            "sub_window": f"vehicle_events_fleet:{sw}",
            "chunk_index": chunk_index,
            "chunk_total_estimated": chunk_total_estimated,
            "chunk_start_ts": current.isoformat(),
            "chunk_end_ts": request_end.isoformat(),
            "chunk_exclusive_end_ts": chunk_end.isoformat(),
            "chunk_duration_minutes": round((request_end - current).total_seconds() / 60, 3),
            "configured_chunk_minutes": round(current_chunk_delta.total_seconds() / 60, 3),
            "min_chunk_minutes": round(min_chunk_delta.total_seconds() / 60, 3),
            "limit": limit,
            "max_pages": max_pages,
            "timeout_s": timeout_s,
            "total_chunks_processed": chunks_processed,
            "total_events_fetched": total_events_fetched,
        }
        try:
            events, stats = provider.fetch_vehicle_events_fleet(
                start_timestamp=current,
                end_timestamp=request_end,
                sub_window_label=f"vehicle_events_fleet:{sw}",
                limit=limit,
                max_pages=max_pages,
                log_context=chunk_context,
                timeout_s=timeout_s,
                return_stats=True,
            )
            if stats.get("stopped_by_max_pages"):
                raise TelematicsProviderSafetyError(
                    "VEHICLE_EVENTS_MAX_PAGES_REACHED",
                    "Fleet vehicle-events page cap reached before chunk completed",
                    context={
                        **chunk_context,
                        "pages_fetched": stats.get("pages_fetched"),
                        "records_fetched": stats.get("records_fetched"),
                        "stopped_by_max_pages": True,
                    },
                )
        except TelematicsProviderSafetyError as exc:
            if _vehicle_events_error_can_reduce_chunk(exc) and current_chunk_delta > min_chunk_delta:
                old_delta = current_chunk_delta
                current_chunk_delta = _halve_timedelta(current_chunk_delta, minimum=min_chunk_delta)
                chunks_reduced += 1
                log_fn(
                    "WARNING",
                    "Fleet vehicle events chunk failed; reducing chunk size",
                    {
                        **chunk_context,
                        **exc.context,
                        "abort_code": exc.code,
                        "old_chunk_minutes": round(old_delta.total_seconds() / 60, 3),
                        "new_chunk_minutes": round(current_chunk_delta.total_seconds() / 60, 3),
                        "chunk_size_reduced": True,
                        "chunk_reductions_total": chunks_reduced,
                        "retry_count": exc.context.get("attempts"),
                    },
                )
                continue

            log_fn(
                "WARNING" if _vehicle_events_error_can_fallback(exc) else "ERROR",
                "Fleet vehicle events chunk failed at minimum/safety limit",
                {
                    **chunk_context,
                    **exc.context,
                    "abort_code": exc.code,
                    "chunk_size_reduced": False,
                    "retry_count": exc.context.get("attempts"),
                },
            )
            if _vehicle_events_error_can_fallback(exc):
                events, fallback_stats = _fetch_vehicle_events_registration_fallback_chunk(
                    provider=provider,
                    start_ts=current,
                    end_ts=request_end,
                    registrations=fallback_registrations,
                    config=fallback_config,
                    limit=limit,
                    max_pages=max_pages,
                    timeout_s=timeout_s,
                    log_fn=log_fn,
                    failed_fleet_error=exc,
                    fallback_state=fallback_state,
                )
                chunks_processed += 1
                total_events_fetched += len(events)
                total_pages_fetched += int(fallback_stats.get("pages_fetched") or 0)
                chunk_stats = {
                    **chunk_context,
                    **fallback_stats,
                    "retry_count": exc.context.get("attempts"),
                    "chunk_size_reduced": False,
                    "total_chunks_processed": chunks_processed,
                    "fleet_chunks_successful": fleet_chunks_successful,
                    "fallback_chunks_successful": fallback_state.get("chunks_successful", 0),
                    "total_pages_fetched": total_pages_fetched,
                    "total_events_fetched": total_events_fetched,
                }
                yield events, chunk_stats
                current = chunk_end
                continue

            _raise_incomplete_event_enrichment(
                log_fn=log_fn,
                message="Fleet vehicle-events chunk failed and registration fallback is not eligible",
                context={
                    **chunk_context,
                    **exc.context,
                    "abort_code": exc.code,
                    "chunk_size_reduced": False,
                    "retry_count": exc.context.get("attempts"),
                },
            )

        chunks_processed += 1
        fleet_chunks_successful += 1
        total_events_fetched += len(events)
        total_pages_fetched += int(stats.get("pages_fetched") or 0)
        chunk_stats = {
            **chunk_context,
            "source": "fleet",
            "status": "success",
            "pages_fetched": stats.get("pages_fetched"),
            "records_fetched": stats.get("records_fetched"),
            "retry_count": 0,
            "chunk_size_reduced": False,
            "total_chunks_processed": chunks_processed,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_state.get("chunks_successful", 0),
            "total_pages_fetched": total_pages_fetched,
            "total_events_fetched": total_events_fetched,
            "complete_event_enrichment": True,
        }
        log_fn("INFO", "Fleet vehicle events adaptive chunk fetched", chunk_stats)
        yield events, chunk_stats
        current = chunk_end

    log_fn(
        "INFO",
        "Fleet vehicle events adaptive fetch complete",
        {
            "endpoint": "/vehicles/events",
            "window_start_ts": start_ts.isoformat(),
            "window_end_ts": end_ts.isoformat(),
            "total_chunks_processed": chunks_processed,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_state.get("chunks_successful", 0),
            "fallback_chunks_started": fallback_state.get("chunks_started", 0),
            "fallback_requests": fallback_state.get("requests", 0),
            "fallback_events_fetched": fallback_state.get("events_fetched", 0),
            "fallback_subchunks_fetched": fallback_state.get("subchunks_fetched", 0),
            "chunk_reductions_total": chunks_reduced,
            "total_pages_fetched": total_pages_fetched,
            "total_events_fetched": total_events_fetched,
            "final_chunk_minutes": round(current_chunk_delta.total_seconds() / 60, 3),
            "complete_event_enrichment": True,
        },
    )


def _compute_rpm_counts(
    *,
    trips: List[Dict[str, Any]],
    notifications: List[Dict[str, Any]],
) -> Tuple[Dict[int, Dict[str, int]], Dict[str, int]]:
    """Count HIGH_RPM and OVERREV notifications per trip with OR matching.

    Match rule (since v1.2 — fallback for missing vehicle_id):

        (
          notification.vehicle_id == trip.vehicle_id
          OR
          normalize(notification.registration) == normalize(trip.registration)
        )
        AND
        trip.start_ts <= notification.event_ts <= trip.end_ts

    The OR-fallback fixes a real gap: a sizeable share of Telematics
    notifications carry a `registration` but no `vehicle_id`. With the
    earlier strict-vehicle-id match, those events were silently dropped
    on the floor and the new RPM columns stayed at zero for whole
    fleets. Registration is normalized via `_normalize_registration`
    (upper + strip) so trivial casing/whitespace differences don't
    block a match.

    Performance shape (preserved from the strict-vehicle implementation):

      1. RPM-typed notifications are pre-grouped twice in a single pass:
         once by `vehicle_id` (when present), once by normalized
         `registration` (when present). Each event carries a stable
         dedupe key (`_notification_dedupe_key`) so we can dedupe
         events that match a trip via BOTH paths.
      2. Each per-key bucket is sorted by `event_ts`, allowing the
         per-trip scan to early-break when the upper window edge is
         exceeded.
      3. Per trip, we walk its vehicle bucket first, then its
         registration bucket while skipping already-seen dedupe keys.
         Total work stays roughly
            O(N + sum_per_key(trips_for_key * events_for_key))
         and stays sub-O(trips * notifications) on any realistic data.

    Per-trip dedupe is keyed on `_notification_dedupe_key(notification)`
    (preferred: `provider_notification_id`; fallback: deterministic
    composite of type / vehicle / registration / event_ts / msg).
    Identity-by-list-index would drift between runs because provider
    pagination does not promise stable ordering — see the helper's
    docstring for the full rationale.

    Returns
    -------
    counts : { provider_trip_id: {"high_rpm": int, "overrev": int} }
        Every trip in `trips` is present in the result. Trips missing
        timestamps OR (vehicle_id AND registration) yield zero counts;
        the caller writes the literal zero, not NULL — "0 events
        observed" is itself a signal. NULLs only appear on historical
        rows that pre-date the migration, never on rows produced here.

    stats : flat dict — used by `run()` for the diagnostic log block
        (see "RPM matching stats"). Keys:
          * "high_rpm_events"        — # notifications classed as HIGH_RPM
                                       (post-type filter, before matching)
          * "overrev_events"         — # notifications classed as OVERREV
          * "vehicles_with_events"   — # distinct vehicle_id buckets
          * "registrations_with_events" — # distinct registration buckets
                                          (after normalize)
          * "matched_high_rpm"       — # HIGH_RPM events matched to a trip
                                       (counted ONCE even if matched via
                                        both vehicle_id and registration)
          * "matched_overrev"        — same, for OVERREV
          * "trips_with_events"      — # trips with high_rpm + overrev > 0
          * "trips_without_events"   — # trips with high_rpm + overrev == 0
          * "matches_via_vehicle_id" — # event-trip matches resolved via
                                       the vehicle_id index
          * "matches_via_registration" — # event-trip matches that were
                                         ONLY resolvable via registration
                                         (i.e. would have been lost under
                                         the old strict-vehicle rule).
    """
    by_vehicle: Dict[Any, List[Tuple[str, Dict[str, Any]]]] = {}
    by_registration: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

    high_rpm_events = 0
    overrev_events = 0

    for n in notifications:
        ntype = n.get("type")
        if ntype != _RPM_TYPE_HIGH_RPM and ntype != _RPM_TYPE_OVERREV:
            continue
        if n.get("event_ts") is None:
            continue

        if ntype == _RPM_TYPE_HIGH_RPM:
            high_rpm_events += 1
        else:
            overrev_events += 1

        dedupe_key = _notification_dedupe_key(n)

        vid = n.get("vehicle_id")
        if vid is not None:
            by_vehicle.setdefault(vid, []).append((dedupe_key, n))

        reg = _normalize_registration(n.get("registration"))
        if reg:
            by_registration.setdefault(reg, []).append((dedupe_key, n))

    for events in by_vehicle.values():
        events.sort(key=lambda e: e[1]["event_ts"])
    for events in by_registration.values():
        events.sort(key=lambda e: e[1]["event_ts"])

    counts: Dict[int, Dict[str, int]] = {}
    matched_high_rpm = 0
    matched_overrev = 0
    matches_via_vehicle = 0
    matches_via_registration = 0
    trips_with_events = 0
    trips_without_events = 0

    for t in trips:
        provider_trip_id = int(t["provider_trip_id"])
        rpm = {"high_rpm": 0, "overrev": 0}
        counts[provider_trip_id] = rpm

        start_ts = t.get("start_ts")
        end_ts = t.get("end_ts")
        if start_ts is None or end_ts is None or end_ts < start_ts:
            trips_without_events += 1
            continue

        vid = t.get("vehicle_id")
        reg = _normalize_registration(t.get("registration"))

        seen_keys: set = set()

        if vid is not None:
            for dedupe_key, n in by_vehicle.get(vid, ()):
                ets = n["event_ts"]
                if ets > end_ts:
                    break
                if ets < start_ts:
                    continue
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                if n["type"] == _RPM_TYPE_HIGH_RPM:
                    rpm["high_rpm"] += 1
                    matched_high_rpm += 1
                else:
                    rpm["overrev"] += 1
                    matched_overrev += 1
                matches_via_vehicle += 1

        if reg:
            for dedupe_key, n in by_registration.get(reg, ()):
                ets = n["event_ts"]
                if ets > end_ts:
                    break
                if ets < start_ts:
                    continue
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                if n["type"] == _RPM_TYPE_HIGH_RPM:
                    rpm["high_rpm"] += 1
                    matched_high_rpm += 1
                else:
                    rpm["overrev"] += 1
                    matched_overrev += 1
                matches_via_registration += 1

        if rpm["high_rpm"] + rpm["overrev"] > 0:
            trips_with_events += 1
        else:
            trips_without_events += 1

    stats = {
        "high_rpm_events": high_rpm_events,
        "overrev_events": overrev_events,
        "vehicles_with_events": len(by_vehicle),
        "registrations_with_events": len(by_registration),
        "matched_high_rpm": matched_high_rpm,
        "matched_overrev": matched_overrev,
        "trips_with_events": trips_with_events,
        "trips_without_events": trips_without_events,
        "matches_via_vehicle_id": matches_via_vehicle,
        "matches_via_registration": matches_via_registration,
    }
    return counts, stats


def _run(client, run_id: str, params: dict, recorder: ExecutionOutcomeRecorder):
    job_started_at = time.monotonic()
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = params.get("client_id")
    if not client_id:
        raise ValueError("Missing required param: client_id")
    window_start_ts_raw = params.get("window_start_ts")
    window_end_ts_raw = params.get("window_end_ts")
    if not window_start_ts_raw or not window_end_ts_raw:
        raise ValueError("Missing required params: window_start_ts, window_end_ts")

    skip_fuel = _param_bool(params, "skip_fuel", False)
    skip_vehicle_events = _param_bool(params, "skip_vehicle_events", False)
    trips_pagination_mode = _trips_pagination_mode(params)
    event_enrichment_mode = _event_enrichment_mode(params)
    event_fetch_strategy = _event_fetch_strategy(params)
    event_enrichment_disabled = event_enrichment_mode == VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED
    schedule_run_type = str(params.get("schedule_run_type") or "").strip().upper()
    vehicle_events_scope = _vehicle_events_scope(params)

    window_start_ts = _parse_runner_iso_ts(str(window_start_ts_raw))
    window_end_ts = _parse_runner_iso_ts(str(window_end_ts_raw))
    if window_end_ts < window_start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")
    trips_chunk_days = _trips_chunk_days(params)
    trips_fetch_chunks = _build_trip_fetch_chunks(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        chunk_days=trips_chunk_days,
    )

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Loading client_account + config from platform control-plane",
        run_id=run_id,
        context={
            "client_id": client_id,
            "window_start_ts": window_start_ts.isoformat(),
            "window_end_ts": window_end_ts.isoformat(),
            **_window_business_context(window_start_ts, window_end_ts),
        },
    )

    cfg = load_client_account_config(client_id=client_id)
    # The provider secret is deliberately NOT resolved here. `cfg` carries only
    # a *reference* (CONVENTIONS.md §7), and the reference is all that is needed
    # to reach the disabled-schedule decision below. Resolving the secret at
    # this point meant an unauthorized or malformed manual-recovery request had
    # already caused the credential to be read out of the environment or secret
    # file before any authority was validated. Resolution now happens once,
    # after the schedule/authority decision, immediately before the provider
    # client that needs it is constructed.
    client_code = cfg.client_code
    recorder.bind_target(
        client_id=client_id,
        client_code=client_code,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        platform_run_id=run_id,
    )
    trip_metrics_population_source = cfg.trip_metrics_population_source
    api_owns_trip_metrics = is_required_trip_metrics_source(
        trip_metrics_population_source,
        TRIP_METRICS_SOURCE_API,
    )
    trip_metrics_source_context = {
        "trip_metrics_population_source": trip_metrics_population_source,
        "required_trip_metrics_population_source": TRIP_METRICS_SOURCE_API,
        "event_derived_metric_population_enabled": api_owns_trip_metrics,
    }
    trip_metrics_skip_context = (
        {}
        if api_owns_trip_metrics
        else trip_metrics_source_skip_context(trip_metrics_population_source, TRIP_METRICS_SOURCE_API)
    )

    schedule = load_dataset_schedule(client_id=client_id, dataset_name=DATASET_NAME)
    if not schedule.exists:
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "No client_dataset_schedule row found; using legacy defaults "
            "(enabled=true, overwrite_existing=true). Seed via onboarding to silence.",
            run_id=run_id,
            context={"client_id": client_id, "dataset_name": DATASET_NAME},
        )
    # `getattr` rather than attribute access: the schedule object is supplied by
    # the loader, and a caller that provides a reduced stand-in must still reach
    # the guard below. A schedule with no identity can never satisfy the
    # manual-recovery authority, which is the intended fail-closed behavior.
    #
    # WHICH schedule identity the terminal record carries (M6/M7).
    #   `load_dataset_schedule` is scoped to the BASE row by design (M5): the
    #   base row is this job's *configuration*, and that stays true for every
    #   cadence. But the execution record states which fire this execution IS,
    #   and for a reconciliation fire that is the reconciliation row, not the
    #   base one. The dispatcher verifies the record against the schedule that
    #   claimed the fire, so binding the base id would make every
    #   WEEKLY/MONTHLY fire fail `EXECUTION_OUTCOME_IDENTITY_MISMATCH` while
    #   DAILY passed only because its two ids coincide.
    #
    #   Honoured only for `trigger=SCHEDULED`, so the manual-recovery path keeps
    #   binding the authoritative base row that `recover_telematics_trips_window`
    #   resolves and verifies against — that identity contract is unchanged, and
    #   no non-dispatcher caller can redirect it.
    _base_schedule_id = getattr(schedule, "schedule_id", None)
    _fired_schedule_id = _base_schedule_id
    if str(params.get("trigger") or "") == "SCHEDULED":
        _param_schedule_id = params.get("schedule_id")
        if _param_schedule_id not in (None, ""):
            _fired_schedule_id = str(_param_schedule_id)
    recorder.bind_schedule(_fired_schedule_id)

    manual_recovery_evidence: Optional[Dict[str, Any]] = None
    if not schedule.enabled:
        # Two very different situations meet here.
        #
        # An ordinary invocation against a disabled schedule must skip and do no
        # provider work — unchanged behavior. What *is* new is that the skip is
        # now stated in the structured terminal record, so no downstream tool can
        # ever again read the process's return code 0 as evidence that business
        # work happened.
        #
        # An invocation that claims manual-recovery authority is validated to the
        # letter against its job parameters, the out-of-band launch attestation
        # and the durable recovery row — the recovery row being the substantive
        # gate, since the attestation carries no secret and proves no provenance.
        # Anything short of full agreement raises, failing the run; it is never
        # quietly downgraded to a skip, because a half-formed authority is an
        # operator error that must be seen.
        if not manual_recovery_authority.authority_requested(params):
            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Dataset schedule disabled; skipping run.",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "dataset_name": DATASET_NAME,
                    "schedule_id": schedule.schedule_id,
                    "manual_recovery_authority": "absent",
                    "provider_requests": 0,
                    "business_transaction_entered": False,
                },
            )
            recorder.record_skipped(reason=SKIP_REASON_DISABLED_SCHEDULE)
            return

        manual_recovery_evidence = (
            manual_recovery_authority.authorize_disabled_schedule_recovery(
                params=params,
                schedule=schedule,
                client_id=client_id,
                dataset_name=DATASET_NAME,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                recovery_row_loader=load_manual_recovery_claim,
            )
        )
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "Dataset schedule disabled; executing under validated "
            "manual-recovery authority.",
            run_id=run_id,
            context={
                "client_id": client_id,
                "dataset_name": DATASET_NAME,
                **manual_recovery_evidence,
            },
        )

    overwrite_existing = schedule.overwrite_existing

    synced_at = datetime.now(timezone.utc)
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Run-level synced_at fixed for this run.",
        run_id=run_id,
        context={
            "client_id": client_id,
            "dataset_name": DATASET_NAME,
            "synced_at": synced_at.isoformat(),
            "synced_at_local": format_business_timestamp(synced_at),
            "overwrite_existing": overwrite_existing,
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            "trips_pagination_mode": trips_pagination_mode,
            **trip_metrics_source_context,
            **trip_metrics_skip_context,
            **_window_business_context(window_start_ts, window_end_ts),
        },
    )

    if api_owns_trip_metrics and not event_enrichment_disabled:
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "Speed bucket thresholds are applied directly to raw vehicle event `speed`; "
            "provider speed unit/meaning is still proof-of-data dependent.",
            run_id=run_id,
            context={"speed_event_source": "/vehicles/events", **trip_metrics_source_context},
        )

    safety_limits = SafetyLimits()
    provider_budget = ProviderRunBudget(limits=safety_limits)
    vehicle_events_timeout_s = _vehicle_events_timeout_s(safety_limits)
    vehicle_events_rate_limit_rps = _vehicle_events_rate_limit_rps()
    registration_fallback_config = _registration_fallback_config(safety_limits)
    best_effort_config = _best_effort_config()

    def _provider_log(level: str, message: str, context: dict) -> None:
        client.log(level, "SCRIPT", JOB_SOURCE, message, run_id=run_id, context=context)

    # Authority-before-secret ordering. Everything above this line is control
    # plane and local validation: an ordinary invocation against a disabled
    # schedule has already returned, and a manual-recovery invocation has
    # already been validated to the letter against its parameters, the launch
    # attestation and the durable RUNNING recovery row. Only an authorized run
    # reaches this statement, so an unauthorized or malformed one resolves no
    # provider credential, decrypts nothing and constructs no provider client.
    # Ordinary enabled scheduled execution is unaffected: it reaches here on
    # every run, exactly as before, and still resolves the secret exactly once.
    provider_password = resolve_secret(cfg.provider_basic_auth_password_secret_ref)

    telematics = TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=provider_password,
        page_limit=provider_page_limit_from_env(),
        safety_limits=safety_limits,
        budget=provider_budget,
        log_fn=_provider_log,
        rate_limit_rps=vehicle_events_rate_limit_rps,
        trips_pagination_mode=trips_pagination_mode,
    )
    # ---- M4 request evidence ----
    # Collected only for the compatibility `/trips` mode, which is the only mode
    # whose success may advance a coverage watermark and therefore the only one
    # that owes a completeness proof. `strict_meta` keeps its historical
    # behaviour exactly: no collector, no sink on the provider client, no extra
    # field in the terminal record beyond the null.
    trips_request_evidence: Optional[RequestEvidenceCollector] = None
    if trips_pagination_mode == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1:
        trips_request_evidence = RequestEvidenceCollector(
            endpoint="/trips",
            effective_window_start_ts=window_start_ts,
            effective_window_end_ts=window_end_ts,
        )
        telematics.request_evidence = trips_request_evidence

    provider_metrics_at_job_start = telematics.metrics_snapshot()
    perf_base_context = {
        "client_id": client_id,
        "run_id": run_id,
        "window_start_ts": window_start_ts.isoformat(),
        "window_end_ts": window_end_ts.isoformat(),
        **_window_business_context(window_start_ts, window_end_ts),
    }

    # ---- 1) Fetch provider trips ----
    incl_private = True
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "GET /trips request mode",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/trips",
            "window_start_ts": window_start_ts.isoformat(),
            "window_end_ts": window_end_ts.isoformat(),
            "incl_private": incl_private,
            "chunk_days": trips_chunk_days,
            "max_chunk_days": TRIPS_MAX_CHUNK_DAYS,
            "chunk_count": len(trips_fetch_chunks),
            "page_limit": telematics.page_limit,
            "trips_pagination_mode": trips_pagination_mode,
            "safety_max_pages_per_subwindow": safety_limits.max_pages_per_subwindow,
            "safety_max_requests_per_run": safety_limits.max_requests_per_run,
            "safety_max_requests_per_endpoint": safety_limits.max_requests_per_endpoint,
            "safety_max_requests_per_subwindow": safety_limits.max_requests_per_subwindow,
        },
    )
    client.log("INFO", "SCRIPT", JOB_SOURCE, "Phase start: trips fetch",
               run_id=run_id, context={
                   "client_id": client_id,
                   "phase": "fetch_trips",
                   "chunk_days": trips_chunk_days,
                   "chunk_count": len(trips_fetch_chunks),
               })
    client.log("INFO", "SCRIPT", JOB_SOURCE, "Fetching provider trips with mandatory job-level chunks",
               run_id=run_id, context={
                   "client_id": client_id,
                   "endpoint": "/trips",
                   "chunk_days": trips_chunk_days,
                   "max_chunk_days": TRIPS_MAX_CHUNK_DAYS,
                   "chunk_count": len(trips_fetch_chunks),
               })
    # From here on the run has genuinely entered provider execution. Recorded
    # before the first request so a safety stop mid-fetch still reports it.
    recorder.mark_provider_entered()
    trips_fetch_started_at = time.monotonic()
    trips_metrics_before = telematics.metrics_snapshot()
    trips_pages_before = provider_budget.total_pages_fetched
    try:
        trips_raw, trips_chunk_summaries = _fetch_trips_in_chunks(
            telematics=telematics,
            client=client,
            run_id=run_id,
            client_id=client_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            chunk_days=trips_chunk_days,
            incl_private=incl_private,
            evidence=trips_request_evidence,
        )
    except TelematicsProviderSafetyError as e:
        # Record what the run had proved before it stopped. It cannot advance
        # coverage — the outcome is FAILED and rc != 0 — but the partial tiling
        # is what makes the stop diagnosable, and building it here keeps the
        # FAILED record's shape identical to the successful one.
        if trips_request_evidence is not None:
            recorder.record_window_completeness(trips_request_evidence.build())
        client.log("ERROR", "SCRIPT", JOB_SOURCE, f"Telematics provider safety stop: {e.code}",
                   run_id=run_id, context={**e.context, "abort_code": e.code, "phase": "fetch_trips"})
        raise
    # M4: the fetch returned, so the tiling this execution attempted is final.
    # Frozen here, before any persistence decision, so the proof describes the
    # provider work and cannot be influenced by what the business transaction
    # later did. `first_seen_request_ids` is the provider_trip_id -> request_id
    # binding used only for rows this run INSERTS.
    # M-LAG: the identity and the observation instant travel together as one
    # first-seen event. `client_trips` carries a CHECK that both are present or
    # both are absent, so they cannot be populated independently even by mistake.
    trips_first_seen: Dict[int, Tuple[str, datetime]] = {}
    if trips_request_evidence is not None:
        trips_window_completeness = trips_request_evidence.build()
        recorder.record_window_completeness(trips_window_completeness)
        trips_first_seen = trips_request_evidence.first_seen_observations()
        # M4 cross-database handoff, step 1 of 2. The request facts become
        # durable in the platform database NOW — before the business
        # transaction below can commit a trip row carrying
        # `first_seen_request_id`. That ordering is what makes an immutable
        # first-seen identity safe to store: by the time a trip can reference
        # `R`, `R` already exists and will still exist even if coverage
        # finalization later fails or the process dies.
        #
        # These rows are PENDING. They are request facts, not coverage
        # evidence: migration 061's CHECK constraints forbid a PENDING row from
        # carrying a fire attribution or any completeness claim, and only the
        # dispatcher may promote them. Writing them therefore cannot advance a
        # watermark or make an incomplete window look complete.
        #
        # A failure here fails the run before anything is committed to the
        # business database, which is the correct direction: no trip row can
        # then exist referencing a request fact that was never stored.
        _persist_pending_request_facts(
            client=client,
            run_id=run_id,
            client_id=client_id,
            platform_run_id=run_id,
            completeness=trips_window_completeness,
        )
    client.log("INFO", "SCRIPT", JOB_SOURCE, f"Fetched trips: {len(trips_raw)}",
               run_id=run_id, context={
                   "client_id": client_id,
                   "chunk_days": trips_chunk_days,
                   "chunk_count": len(trips_chunk_summaries),
                   "trips_fetched": len(trips_raw),
                   "first_seen_request_bindings": len(trips_first_seen),
               })
    client.log("INFO", "SCRIPT", JOB_SOURCE, "Phase end: trips fetch",
               run_id=run_id, context={
                   "client_id": client_id,
                   "phase": "fetch_trips",
                   "trips_fetched": len(trips_raw),
                   "chunk_days": trips_chunk_days,
                   "chunk_count": len(trips_chunk_summaries),
               })
    trips_metrics_after = telematics.metrics_snapshot()
    trips_fetch_elapsed_s = _elapsed_seconds(trips_fetch_started_at)
    trips_pages_fetched = provider_budget.total_pages_fetched - trips_pages_before
    trips_api_request_elapsed_s = round(
        _provider_metric_delta(
            trips_metrics_before,
            trips_metrics_after,
            "request_elapsed_seconds_by_endpoint",
            "/trips",
        ),
        3,
    )
    trips_response_parse_elapsed_s = round(
        _provider_metric_delta(
            trips_metrics_before,
            trips_metrics_after,
            "response_parse_elapsed_seconds_by_endpoint",
            "/trips",
        ),
        3,
    )
    trips_api_requests = _provider_metric_count_delta(trips_metrics_before, trips_metrics_after, "/trips")
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: /trips fetch",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "fetch_trips",
            "endpoint": "/trips",
            "elapsed_seconds": trips_fetch_elapsed_s,
            "api_request_elapsed_seconds": trips_api_request_elapsed_s,
            "response_parse_elapsed_seconds": trips_response_parse_elapsed_s,
            "api_requests": trips_api_requests,
            "pages_fetched": trips_pages_fetched,
            "records_fetched": len(trips_raw),
            "chunk_days": trips_chunk_days,
            "chunk_count": len(trips_chunk_summaries),
            "limit": telematics.page_limit,
            "records_per_second": _records_per_second(len(trips_raw), trips_fetch_elapsed_s),
        },
    )

    # ---- 2) Parse trips, extract locations ----
    trips_for_matching: List[Dict[str, Any]] = []
    trips_parsed: List[Dict[str, Any]] = []
    # Global Client Trips admission rule. One gate instance per run: it decides
    # at parse time and it is asked again at the persistence boundary, so the
    # over-cap trip is discarded as early as safely possible AND cannot reach
    # the upsert batch through a later edit of the row-preparation loop.
    trips_admission = client_trips_admission.ClientTripsAdmission()
    trip_parse_diagnostics = {
        "trips_provider_rows_fetched": len(trips_raw),
        "trips_rows_parsed": 0,
        "trips_rows_skipped_missing_registration": 0,
        "trips_rows_malformed_trip_id": 0,
        "trips_rows_malformed_timestamp": 0,
        "trips_rows_other_parse_error": 0,
        client_trips_admission.REJECTED_COUNTER_KEY: 0,
        # Defense in depth. The parse gate above already removed every over-cap
        # trip, so this counter is expected to stay 0 for the lifetime of this
        # code; a non-zero value means a row reached the persistence boundary
        # without passing the parse gate.
        "client_trips_rejected_distance_over_2000km_at_persistence": 0,
        "trips_rows_prepared_for_upsert": 0,
        "trips_rows_upserted": None,
        "trips_private_count": 0,
        "trips_business_count": 0,
        "trips_unknown_mode_count": 0,
    }
    trip_parse_diagnostics_logged = False

    def _log_trip_parse_diagnostics() -> None:
        nonlocal trip_parse_diagnostics_logged
        if trip_parse_diagnostics_logged:
            return
        trip_parse_diagnostics_logged = True
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Provider trip parse diagnostics",
            run_id=run_id,
            context={
                "client_id": client_id,
                **trip_parse_diagnostics,
            },
        )

    trip_parse_started_at = time.monotonic()
    for t in trips_raw:
        provider_trip_id: Optional[int] = None
        try:
            if not isinstance(t, dict):
                exc = TypeError(f"provider trip row is not an object: {type(t).__name__}")
                trip_parse_diagnostics["trips_rows_other_parse_error"] += 1
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Malformed provider trip row: unexpected parse error",
                    run_id=run_id,
                    context=_trip_diagnostic_context(
                        t,
                        reason="other_parse_error",
                        provider_trip_id=None,
                        error=exc,
                    ),
                )
                _log_trip_parse_diagnostics()
                raise exc

            try:
                provider_trip_id = int(t["trip_id"])
            except (KeyError, TypeError, ValueError) as exc:
                trip_parse_diagnostics["trips_rows_malformed_trip_id"] += 1
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Malformed provider trip row: invalid trip_id",
                    run_id=run_id,
                    context=_trip_diagnostic_context(
                        t,
                        reason="malformed_trip_id",
                        provider_trip_id=t.get("trip_id"),
                        error=exc,
                    ),
                )
                _log_trip_parse_diagnostics()
                raise

            # ---- Client Trips global admission rule -----------------------
            # Provider distance > 2,000 km is discarded here, before anything
            # else looks at the row: the trip is not going to be persisted, so
            # it must not enter `trips_for_matching` either (it would otherwise
            # consume event enrichment and, with a missing registration, could
            # abort a run for a trip the platform has already decided to drop).
            # Rejections are aggregated on `trips_admission`, never logged per
            # trip.
            if not trips_admission.admits_provider_trip(
                t, provider_trip_id=provider_trip_id
            ):
                trip_parse_diagnostics[
                    client_trips_admission.REJECTED_COUNTER_KEY
                ] += 1
                continue

            registration = t.get("registration")
            if not registration:
                trip_parse_diagnostics["trips_rows_skipped_missing_registration"] += 1
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Skipping provider trip with missing registration",
                    run_id=run_id,
                    context=_trip_diagnostic_context(
                        t,
                        reason="missing_registration",
                        provider_trip_id=provider_trip_id,
                    ),
                )
                continue

            try:
                start_ts = _parse_provider_dt(t.get("start_timestamp"))
                end_ts = _parse_provider_dt(t.get("end_timestamp"))
            except (TypeError, ValueError) as exc:
                trip_parse_diagnostics["trips_rows_malformed_timestamp"] += 1
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Malformed provider trip row: invalid timestamp",
                    run_id=run_id,
                    context=_trip_diagnostic_context(
                        t,
                        reason="malformed_timestamp",
                        provider_trip_id=provider_trip_id,
                        error=exc,
                    ),
                )
                _log_trip_parse_diagnostics()
                raise

            start_lat, start_lon = _extract_coords(t.get("start_coordinates"))
            end_lat, end_lon = _extract_coords(t.get("end_coordinates"))
            trip_mode = _trip_mode_from_provider(t)
            if trip_mode == "private":
                trip_parse_diagnostics["trips_private_count"] += 1
            elif trip_mode == "business":
                trip_parse_diagnostics["trips_business_count"] += 1
            else:
                trip_parse_diagnostics["trips_unknown_mode_count"] += 1

            # Odometer extraction is best-effort: the canonical provider
            # field names per spec are start/end_odometer_value, but real
            # payloads have been seen to use the legacy synonyms below.
            # Missing or malformed values stay NULL — see migration 016 for
            # the column shape and rationale.
            start_odo = _extract_odometer_value(
                t, "start_odometer_value", "start_odometer", "odometer_start",
            )
            end_odo = _extract_odometer_value(
                t, "end_odometer_value", "end_odometer", "odometer_end",
            )

            parsed = {
                "provider_trip_id": provider_trip_id,
                "registration": registration,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "start_location": t.get("start_location"),
                "start_latitude": start_lat,
                "start_longitude": start_lon,
                "end_location": t.get("end_location"),
                "end_latitude": end_lat,
                "end_longitude": end_lon,
                "trip_distance_meters": t.get("trip_distance"),
                "start_odometer_value": start_odo,
                "end_odometer_value": end_odo,
                "driver_tag_description": _extract_optional_text(
                    t,
                    "driver_tag_description",
                    "driver_tag_desc",
                    "driver_tag",
                    "tag_description",
                ),
                "driver_id": _extract_optional_text(t, "driver_id", "driver_uuid"),
                "identification_tag_id": _extract_optional_text(
                    t,
                    "identification_tag_id",
                    "driver_identification_tag_id",
                    "driver_tag_id",
                    "tag_id",
                ),
                "trip_mode": trip_mode,
                "raw": t,
            }
            trips_parsed.append(parsed)

            trips_for_matching.append({
                "provider_trip_id": provider_trip_id,
                "registration": registration,
                "vehicle_id": t.get("vehicle_id"),
                "driver_id": _extract_optional_text(t, "driver_id", "driver_uuid"),
                "driver_name": t.get("driver_name"),
                "driver_surname": t.get("driver_surname"),
                "identification_tag_id": parsed["identification_tag_id"],
                "start_ts": start_ts,
                "end_ts": end_ts,
            })
            trip_parse_diagnostics["trips_rows_parsed"] += 1
        except (KeyError, TypeError, ValueError):
            raise
        except Exception as exc:
            trip_parse_diagnostics["trips_rows_other_parse_error"] += 1
            client.log(
                "WARNING", "SCRIPT", JOB_SOURCE,
                "Malformed provider trip row: unexpected parse error",
                run_id=run_id,
                context=_trip_diagnostic_context(
                    t,
                    reason="other_parse_error",
                    provider_trip_id=provider_trip_id,
                    error=exc,
                ),
            )
            _log_trip_parse_diagnostics()
            raise

    trip_parse_elapsed_s = _elapsed_seconds(trip_parse_started_at)
    _log_trip_parse_diagnostics()
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: trip parsing",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "parse_trips",
            "elapsed_seconds": trip_parse_elapsed_s,
            "records_input": len(trips_raw),
            "records_parsed": len(trips_parsed),
            "records_per_second": _records_per_second(len(trips_raw), trip_parse_elapsed_s),
        },
    )
    if trips_admission.rejected_total:
        # One bounded aggregate per run, never one log per rejected trip.
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "Client Trips admission discarded provider trips over the 2,000 km distance cap",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "parse_trips",
                "max_trip_distance_meters": client_trips_admission.MAX_TRIP_DISTANCE_METERS,
                "rejected_by_reason": trips_admission.rejected_by_reason,
                **trips_admission.summary(),
            },
        )

    if trip_parse_diagnostics["trips_rows_skipped_missing_registration"]:
        if event_enrichment_disabled:
            client.log(
                "WARNING", "SCRIPT", JOB_SOURCE,
                "Continuing after provider trips with missing registration because event enrichment is disabled",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "phase": "parse_trips",
                    "event_enrichment_mode": event_enrichment_mode,
                    **trip_parse_diagnostics,
                },
            )
        else:
            client.log(
                "ERROR", "SCRIPT", JOB_SOURCE,
                "DB upsert skipped because one or more provider trips were not parseable for event enrichment",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "phase": "parse_trips",
                    "abort_code": "TRIPS_SKIPPED_MISSING_REGISTRATION",
                    "complete_event_enrichment": False,
                    **trip_parse_diagnostics,
                },
            )
            raise ValueError("Incomplete trip parse: provider trips with missing registration cannot be safely enriched")

    registrations_by_norm: Dict[str, str] = {}
    for trip in trips_for_matching:
        reg_norm = _normalize_registration(trip.get("registration"))
        if reg_norm and reg_norm not in registrations_by_norm:
            registrations_by_norm[reg_norm] = str(trip["registration"]).strip()
    trip_registrations_norm = set(registrations_by_norm.keys())
    registrations = [registrations_by_norm[k] for k in sorted(registrations_by_norm)]
    vehicle_events_fleet_limit = _vehicle_events_fleet_limit()
    vehicle_events_fleet_max_pages = _vehicle_events_fleet_max_pages()
    vehicle_events_initial_chunk_delta = _vehicle_events_chunk_delta()
    vehicle_events_min_chunk_delta = _vehicle_events_min_chunk_delta()
    if vehicle_events_initial_chunk_delta < vehicle_events_min_chunk_delta:
        vehicle_events_initial_chunk_delta = vehicle_events_min_chunk_delta
    trip_vehicle_ids_norm = {
        _normalize_vehicle_id(trip.get("vehicle_id"))
        for trip in trips_for_matching
        if _normalize_vehicle_id(trip.get("vehicle_id"))
    }

    # Which of the trips we just read are actually new to durable client state.
    #
    # This is a read, before any provider event spend and outside the write
    # transaction, and it is the only sound way to ask the question. The metric
    # columns cannot answer it: `high_rpm_events_count` is 0 on all 220,497 FOXTROT
    # rows and `overrev_events_count` is 0 on all but 1,255, because "enriched,
    # observed nothing" and "never enriched" are the same integer. Row presence
    # is the one signal that distinguishes them.
    #
    # The direction of any staleness is safe. A row this read sees, exists —
    # nothing in this job deletes trips — so a trip can never be misfiled as
    # already-captured and left with false zeros. The opposite drift, a row
    # inserted by another run between this read and our upsert, only adds a
    # candidate we then enrich correctly: wasted work, never wrong data.
    reconciliation_scope_active = (
        vehicle_events_scope == VEHICLE_EVENTS_SCOPE_RECONCILIATION_CANDIDATES
        and api_owns_trip_metrics
        and not event_enrichment_disabled
    )
    existing_provider_trip_ids: set[int] = set()
    candidate_trip_ids: set[int] = set()
    candidate_trips: List[Dict[str, Any]] = []
    candidate_event_windows: List[Dict[str, Any]] = []
    if reconciliation_scope_active and trips_for_matching:
        candidate_probe_started_at = time.monotonic()
        probe_schema = _safe_ident(cfg.client_db_schema)
        probe_conn = _client_business_pg_conn(cfg)
        try:
            probe_conn.read_only = True
            with probe_conn.cursor() as probe_cur:
                probe_cur.execute(
                    f"""
                    SELECT provider_trip_id
                      FROM {probe_schema}.client_trips
                     WHERE client_id = %s::uuid
                       AND provider_trip_id = ANY(%s)
                    """,
                    (
                        client_id,
                        [int(t["provider_trip_id"]) for t in trips_for_matching],
                    ),
                )
                # `_client_business_pg_conn` builds a plain psycopg connection,
                # so rows are tuples. The single projected column is index 0.
                existing_provider_trip_ids = {
                    int(row[0]) for row in probe_cur.fetchall()
                }
        finally:
            probe_conn.close()
        candidate_trips = [
            trip for trip in trips_for_matching
            if int(trip["provider_trip_id"]) not in existing_provider_trip_ids
        ]
        candidate_trip_ids = {int(trip["provider_trip_id"]) for trip in candidate_trips}
        candidate_event_windows = _build_candidate_event_windows(
            candidate_trips=candidate_trips,
            registrations_by_norm=registrations_by_norm,
            coalesce_gap=_vehicle_events_candidate_coalesce_gap(),
        )
        candidate_max_windows = _vehicle_events_candidate_max_windows()
        if len(candidate_event_windows) > candidate_max_windows:
            # Too many candidates for scoping to be the cheap strategy. Hand the
            # whole window back to the fleet scan, which is the historical path
            # and is complete by construction. Note what is being turned off:
            # not just the scoped fetch but the scoped *write*, because once
            # every trip's events have actually been fetched, recomputing every
            # trip's metrics is a real observation rather than the zero-fill
            # this scope exists to prevent.
            client.log(
                "WARNING", "SCRIPT", JOB_SOURCE,
                "Candidate density exceeds the scoped-fetch ceiling; using the full-window fleet scan",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "phase": "resolve_event_scope",
                    "vehicle_events_scope": vehicle_events_scope,
                    "schedule_run_type": schedule_run_type,
                    "candidate_trips": len(candidate_trips),
                    "candidate_event_windows": len(candidate_event_windows),
                    "candidate_max_windows": candidate_max_windows,
                    "fallback_strategy": "full_window_fleet_scan",
                },
            )
            reconciliation_scope_active = False
            candidate_trips = []
            candidate_trip_ids = set()
            candidate_event_windows = []
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Reconciliation event scope resolved to newly captured trips",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "resolve_event_scope",
                "vehicle_events_scope": vehicle_events_scope,
                "schedule_run_type": schedule_run_type,
                "trips_in_window": len(trips_for_matching),
                "trips_already_captured": len(existing_provider_trip_ids),
                "candidate_trips": len(candidate_trips),
                "candidate_event_windows": len(candidate_event_windows),
                "candidate_registrations": len({
                    w["registration_norm"] for w in candidate_event_windows
                }),
                "candidate_event_window_seconds_total": round(sum(
                    (w["end_ts"] - w["start_ts"]).total_seconds()
                    for w in candidate_event_windows
                ), 3),
                "probe_elapsed_seconds": _elapsed_seconds(candidate_probe_started_at),
            },
        )

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase start: vehicle inventory fetch",
        run_id=run_id,
        context={"client_id": client_id, "phase": "fetch_vehicles", "endpoint": "/vehicles"},
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Fetching fleet-wide vehicle inventory for trip metadata enrichment",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/vehicles",
            "page_limit": telematics.page_limit,
            "max_pages": safety_limits.max_pages_per_subwindow,
        },
    )
    vehicles_fetch_started_at = time.monotonic()
    vehicles_metrics_before = telematics.metrics_snapshot()
    vehicles_pages_before = provider_budget.total_pages_fetched
    try:
        vehicle_inventory_rows = telematics.fetch_vehicles_fleet(
            sub_window_label="vehicle_inventory",
        )
    except TelematicsProviderSafetyError as e:
        client.log("ERROR", "SCRIPT", JOB_SOURCE, f"Telematics provider safety stop: {e.code}",
                   run_id=run_id, context={**e.context, "abort_code": e.code, "phase": "fetch_vehicles"})
        raise

    vehicle_metadata_by_id, vehicle_metadata_by_registration = _build_vehicle_metadata_lookups(vehicle_inventory_rows)
    vehicle_metadata_stats = {
        "trips_matched_by_vehicle_id": 0,
        "trips_matched_by_registration_fallback": 0,
        "trips_without_vehicle_metadata_match": 0,
    }
    for trip in trips_for_matching:
        vehicle_id_norm = _normalize_vehicle_id(trip.get("vehicle_id"))
        registration_norm = _normalize_registration(trip.get("registration"))
        if vehicle_id_norm and vehicle_id_norm in vehicle_metadata_by_id:
            vehicle_metadata_stats["trips_matched_by_vehicle_id"] += 1
        elif registration_norm and registration_norm in vehicle_metadata_by_registration:
            vehicle_metadata_stats["trips_matched_by_registration_fallback"] += 1
        else:
            vehicle_metadata_stats["trips_without_vehicle_metadata_match"] += 1

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Fetched fleet-wide vehicle inventory for trip metadata enrichment",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/vehicles",
            "vehicles_fetched": len(vehicle_inventory_rows),
            "lookup_vehicle_ids": len(vehicle_metadata_by_id),
            "lookup_registrations": len(vehicle_metadata_by_registration),
            **vehicle_metadata_stats,
        },
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase end: vehicle inventory fetch",
        run_id=run_id,
        context={
            "client_id": client_id,
            "phase": "fetch_vehicles",
            "endpoint": "/vehicles",
            "vehicles_fetched": len(vehicle_inventory_rows),
        },
    )
    vehicles_metrics_after = telematics.metrics_snapshot()
    vehicles_fetch_elapsed_s = _elapsed_seconds(vehicles_fetch_started_at)
    vehicles_pages_fetched = provider_budget.total_pages_fetched - vehicles_pages_before
    vehicles_api_request_elapsed_s = round(
        _provider_metric_delta(
            vehicles_metrics_before,
            vehicles_metrics_after,
            "request_elapsed_seconds_by_endpoint",
            "/vehicles",
        ),
        3,
    )
    vehicles_response_parse_elapsed_s = round(
        _provider_metric_delta(
            vehicles_metrics_before,
            vehicles_metrics_after,
            "response_parse_elapsed_seconds_by_endpoint",
            "/vehicles",
        ),
        3,
    )
    vehicles_api_requests = _provider_metric_count_delta(vehicles_metrics_before, vehicles_metrics_after, "/vehicles")
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: /vehicles fetch",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "fetch_vehicles",
            "endpoint": "/vehicles",
            "elapsed_seconds": vehicles_fetch_elapsed_s,
            "api_request_elapsed_seconds": vehicles_api_request_elapsed_s,
            "response_parse_elapsed_seconds": vehicles_response_parse_elapsed_s,
            "api_requests": vehicles_api_requests,
            "pages_fetched": vehicles_pages_fetched,
            "records_fetched": len(vehicle_inventory_rows),
            "limit": telematics.page_limit,
            "records_per_second": _records_per_second(len(vehicle_inventory_rows), vehicles_fetch_elapsed_s),
        },
    )

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase start: driver inventory fetch",
        run_id=run_id,
        context={"client_id": client_id, "phase": "fetch_drivers", "endpoint": "/drivers"},
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Fetching fleet-wide driver inventory for license restrictions enrichment",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/drivers",
            "page_limit": telematics.page_limit,
            "max_pages": safety_limits.max_pages_per_subwindow,
        },
    )
    drivers_fetch_started_at = time.monotonic()
    drivers_metrics_before = telematics.metrics_snapshot()
    drivers_pages_before = provider_budget.total_pages_fetched
    try:
        driver_inventory_rows = telematics.fetch_drivers_fleet(
            sub_window_label="driver_inventory",
        )
    except TelematicsProviderSafetyError as e:
        client.log("ERROR", "SCRIPT", JOB_SOURCE, f"Telematics provider safety stop: {e.code}",
                   run_id=run_id, context={**e.context, "abort_code": e.code, "phase": "fetch_drivers"})
        raise

    driver_restriction_lookups = _build_driver_restriction_lookups(driver_inventory_rows)
    driver_restriction_stats = {
        "trips_driver_restrictions_matched_by_driver_id": 0,
        "trips_driver_restrictions_matched_by_identification_tag_id": 0,
        "trips_driver_restrictions_matched_by_driver_name": 0,
        "trips_driver_restrictions_unmatched_identified_driver": 0,
        "trips_driver_restrictions_unidentified_driver": 0,
        "trips_driver_restrictions_with_value": 0,
        "trips_driver_restrictions_without_value": 0,
    }
    for trip in trips_for_matching:
        restrictions, match_source = _driver_restrictions_for_trip(
            driver_id=trip.get("driver_id"),
            identification_tag_id=trip.get("identification_tag_id"),
            driver_name=trip.get("driver_name"),
            driver_surname=trip.get("driver_surname"),
            lookups=driver_restriction_lookups,
        )
        if match_source == "driver_id":
            driver_restriction_stats["trips_driver_restrictions_matched_by_driver_id"] += 1
        elif match_source == "identification_tag_id":
            driver_restriction_stats["trips_driver_restrictions_matched_by_identification_tag_id"] += 1
        elif match_source == "driver_name":
            driver_restriction_stats["trips_driver_restrictions_matched_by_driver_name"] += 1
        elif match_source == "unmatched":
            driver_restriction_stats["trips_driver_restrictions_unmatched_identified_driver"] += 1
        else:
            driver_restriction_stats["trips_driver_restrictions_unidentified_driver"] += 1

        if restrictions is None:
            driver_restriction_stats["trips_driver_restrictions_without_value"] += 1
        else:
            driver_restriction_stats["trips_driver_restrictions_with_value"] += 1

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Fetched fleet-wide driver inventory for license restrictions enrichment",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/drivers",
            "drivers_fetched": len(driver_inventory_rows),
            "lookup_driver_ids": len(driver_restriction_lookups["by_driver_id"]),
            "lookup_identification_tag_ids": len(driver_restriction_lookups["by_identification_tag_id"]),
            "lookup_driver_names": len(driver_restriction_lookups["by_name"]),
            **driver_restriction_stats,
        },
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase end: driver inventory fetch",
        run_id=run_id,
        context={
            "client_id": client_id,
            "phase": "fetch_drivers",
            "endpoint": "/drivers",
            "drivers_fetched": len(driver_inventory_rows),
        },
    )
    drivers_metrics_after = telematics.metrics_snapshot()
    drivers_fetch_elapsed_s = _elapsed_seconds(drivers_fetch_started_at)
    drivers_pages_fetched = provider_budget.total_pages_fetched - drivers_pages_before
    drivers_api_request_elapsed_s = round(
        _provider_metric_delta(
            drivers_metrics_before,
            drivers_metrics_after,
            "request_elapsed_seconds_by_endpoint",
            "/drivers",
        ),
        3,
    )
    drivers_response_parse_elapsed_s = round(
        _provider_metric_delta(
            drivers_metrics_before,
            drivers_metrics_after,
            "response_parse_elapsed_seconds_by_endpoint",
            "/drivers",
        ),
        3,
    )
    drivers_api_requests = _provider_metric_count_delta(drivers_metrics_before, drivers_metrics_after, "/drivers")
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: /drivers fetch",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "fetch_drivers",
            "endpoint": "/drivers",
            "elapsed_seconds": drivers_fetch_elapsed_s,
            "api_request_elapsed_seconds": drivers_api_request_elapsed_s,
            "response_parse_elapsed_seconds": drivers_response_parse_elapsed_s,
            "api_requests": drivers_api_requests,
            "pages_fetched": drivers_pages_fetched,
            "records_fetched": len(driver_inventory_rows),
            "limit": telematics.page_limit,
            "records_per_second": _records_per_second(len(driver_inventory_rows), drivers_fetch_elapsed_s),
        },
    )

    registration_fallback_registrations = _build_registration_fallback_list(
        vehicle_inventory_rows=vehicle_inventory_rows,
        trip_registrations_by_norm=registrations_by_norm,
    )
    if skip_vehicle_events and api_owns_trip_metrics:
        client.log(
            "ERROR", "SCRIPT", JOB_SOURCE,
            "DB upsert skipped because skip_vehicle_events=true would produce incomplete event enrichment",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "abort_code": "SKIP_VEHICLE_EVENTS_INCOMPLETE_ENRICHMENT",
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                "complete_event_enrichment": False,
                **trip_metrics_source_context,
            },
        )
        raise ValueError("skip_vehicle_events=true is incompatible with strict complete event enrichment")

    if not api_owns_trip_metrics:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Event-derived trip metric population skipped by trip_metrics_population_source",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                "registrations_count": len(registrations),
                "speeding_counts_will_be_left_unchanged": True,
                "rpm_counts_will_be_left_unchanged": True,
                "overrev_counts_will_be_left_unchanged": True,
                **trip_metrics_source_context,
                **trip_metrics_skip_context,
            },
        )
    elif event_enrichment_disabled:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Vehicle event enrichment disabled by event_enrichment_mode; skipping /vehicles/events fetch",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                "registrations_count": len(registrations),
                "speeding_counts_will_be_zero": True,
                "rpm_counts_will_be_zero": True,
                "overrev_counts_will_be_zero": True,
                **trip_metrics_source_context,
            },
        )
    else:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Phase start: fleet events fetch",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                **trip_metrics_source_context,
                "registration_fallback_enabled": registration_fallback_config.enabled,
                "registration_fallback_registrations_count": len(registration_fallback_registrations),
                "registration_fallback_rps": registration_fallback_config.rps,
                "registration_fallback_max_chunks": registration_fallback_config.max_chunks,
                "registration_fallback_max_requests_per_run": registration_fallback_config.max_requests_per_run,
                "best_effort_min_fleet_chunk_minutes": round(
                    best_effort_config.min_fleet_chunk_delta.total_seconds() / 60,
                    3,
                ),
                "best_effort_min_registration_chunk_minutes": round(
                    best_effort_config.min_registration_chunk_delta.total_seconds() / 60,
                    3,
                ),
                "best_effort_max_gaps_per_run": best_effort_config.max_gaps_per_run,
                "best_effort_max_split_depth": best_effort_config.max_split_depth,
            },
        )

        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Fetching fleet-wide raw vehicle events with adaptive chunks",
            run_id=run_id,
            context={
                "client_id": client_id,
                "registrations_count": len(registrations),
                "endpoint": "/vehicles/events",
                "limit": vehicle_events_fleet_limit,
                "max_pages_per_chunk": vehicle_events_fleet_max_pages,
                "initial_chunk_minutes": round(vehicle_events_initial_chunk_delta.total_seconds() / 60, 3),
                "min_chunk_minutes": round(vehicle_events_min_chunk_delta.total_seconds() / 60, 3),
                "timeout_s": vehicle_events_timeout_s,
                "rate_limit_rps": vehicle_events_rate_limit_rps,
                "skip_vehicle_events": skip_vehicle_events,
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                **trip_metrics_source_context,
                "registration_fallback_enabled": registration_fallback_config.enabled,
                "registration_fallback_registrations_count": len(registration_fallback_registrations),
                "registration_fallback_rps": registration_fallback_config.rps,
            },
        )
    vehicle_events_raw: List[Dict[str, Any]] = []
    rpm_vehicle_events_raw: List[Dict[str, Any]] = []
    vehicle_events_fetch_started_at = time.monotonic()
    vehicle_events_metrics_before = telematics.metrics_snapshot()
    vehicle_events_pages_fetched = 0
    vehicle_events_chunks_processed = 0
    fleet_rows_fetched = 0
    fleet_rows_after_registration_filter = 0
    fleet_rows_after_speed_filter = 0
    fleet_rows_after_rpm_identity_filter = 0
    fleet_provider_labeled_rpm_rows = 0
    fleet_chunks_successful = 0
    fallback_chunks_successful = 0
    fallback_requests = 0
    fallback_events_fetched = 0
    fallback_subchunks_fetched = 0
    vehicle_event_gaps: List[Dict[str, Any]] = []
    events_fetched_from_fleet = 0
    candidate_scope_requests = 0
    if not api_owns_trip_metrics or event_enrichment_disabled:
        pass
    elif reconciliation_scope_active:
        # Reconciliation scope: buy events for the trips this run discovered,
        # and for nothing else. When it discovered none, that is a complete
        # answer and the correct spend is zero requests — not a 16-day fleet
        # scan that recomputes metrics nobody asked about.
        def _vehicle_events_log(level: str, message: str, context: Dict[str, Any]) -> None:
            client.log(
                level, "SCRIPT", JOB_SOURCE, message,
                run_id=run_id,
                context={"client_id": client_id, **context},
            )

        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Phase start: reconciliation candidate events fetch",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "vehicle_events_scope": vehicle_events_scope,
                "schedule_run_type": schedule_run_type,
                "candidate_trips": len(candidate_trips),
                "candidate_event_windows": len(candidate_event_windows),
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                **trip_metrics_source_context,
            },
        )
        candidate_requests_before = telematics.budget.total_requests
        for window in candidate_event_windows:
            # `fetch_vehicle_events_registration` is the same documented
            # endpoint with a `registration` filter, and it is budgeted under
            # its own key, so a reconciliation cannot spend the fleet scan's
            # allowance. Adaptive splitting is reused rather than reimplemented:
            # a candidate window is normally one trip long, but a merged window
            # over a busy vehicle must still be allowed to halve itself instead
            # of hitting the page cap.
            window_events, window_stats = _fetch_vehicle_events_candidate_window(
                provider=telematics,
                registration=window["registration"],
                start_ts=window["start_ts"],
                end_ts=window["end_ts"],
                limit=vehicle_events_fleet_limit,
                max_pages=vehicle_events_fleet_max_pages,
                timeout_s=vehicle_events_timeout_s,
                min_chunk_delta=vehicle_events_min_chunk_delta,
                max_split_depth=best_effort_config.max_split_depth,
                log_fn=_vehicle_events_log,
                context={
                    "phase": "fetch_vehicle_events_reconciliation_candidates",
                    "registration": window["registration"],
                },
            )
            vehicle_events_chunks_processed += 1
            vehicle_events_pages_fetched += int(window_stats.get("pages_fetched") or 0)
            filtered_events, filter_stats = _filter_fleet_speeding_events(
                vehicle_events=window_events,
                trip_registrations_norm=trip_registrations_norm,
            )
            rpm_events, rpm_filter_stats = _filter_fleet_rpm_vehicle_events(
                vehicle_events=window_events,
                trip_registrations_norm=trip_registrations_norm,
                trip_vehicle_ids_norm=trip_vehicle_ids_norm,
            )
            fleet_rows_fetched += filter_stats["rows_fetched"]
            fleet_rows_after_registration_filter += filter_stats["rows_kept_after_registration_filter"]
            fleet_rows_after_speed_filter += filter_stats["rows_kept_after_speed_filter"]
            fleet_rows_after_rpm_identity_filter += rpm_filter_stats["rows_kept_after_trip_identity_filter"]
            fleet_provider_labeled_rpm_rows += rpm_filter_stats["provider_labeled_rpm_rows_kept"]
            events_fetched_from_fleet += len(window_events)
            vehicle_events_raw.extend(filtered_events)
            rpm_vehicle_events_raw.extend(rpm_events)
        candidate_scope_requests = max(
            0, telematics.budget.total_requests - candidate_requests_before
        )
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Phase end: reconciliation candidate events fetch",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "vehicle_events_scope": vehicle_events_scope,
                "candidate_trips": len(candidate_trips),
                "candidate_event_windows": len(candidate_event_windows),
                "candidate_scope_requests": candidate_scope_requests,
                "pages_fetched": vehicle_events_pages_fetched,
                "events_fetched_total": events_fetched_from_fleet,
                "rows_kept_after_registration_filter": fleet_rows_after_registration_filter,
                "rows_kept_after_rpm_identity_filter": fleet_rows_after_rpm_identity_filter,
                "provider_labeled_rpm_rows_kept": fleet_provider_labeled_rpm_rows,
                **trip_metrics_source_context,
            },
        )
    elif trip_registrations_norm:
        def _vehicle_events_log(level: str, message: str, context: Dict[str, Any]) -> None:
            client.log(
                level, "SCRIPT", JOB_SOURCE, message,
                run_id=run_id,
                context={"client_id": client_id, **context},
            )

        if event_fetch_strategy == VEHICLE_EVENTS_ENRICHMENT_MODE_AUDITED_BEST_EFFORT:
            vehicle_events_iter = _iter_fetch_vehicle_events_fleet_best_effort(
                provider=telematics,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                initial_chunk_delta=vehicle_events_initial_chunk_delta,
                min_chunk_delta=vehicle_events_min_chunk_delta,
                fallback_config=registration_fallback_config,
                fallback_registrations=registration_fallback_registrations,
                best_effort_config=best_effort_config,
                gaps=vehicle_event_gaps,
                limit=vehicle_events_fleet_limit,
                max_pages=vehicle_events_fleet_max_pages,
                timeout_s=vehicle_events_timeout_s,
                log_fn=_vehicle_events_log,
            )
        else:
            vehicle_events_iter = _iter_fetch_vehicle_events_fleet_adaptive(
                provider=telematics,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                initial_chunk_delta=vehicle_events_initial_chunk_delta,
                min_chunk_delta=vehicle_events_min_chunk_delta,
                fallback_config=registration_fallback_config,
                fallback_registrations=registration_fallback_registrations,
                limit=vehicle_events_fleet_limit,
                max_pages=vehicle_events_fleet_max_pages,
                timeout_s=vehicle_events_timeout_s,
                log_fn=_vehicle_events_log,
            )

        for vehicle_events, chunk_fetch_stats in vehicle_events_iter:
            vehicle_events_chunks_processed += 1
            vehicle_events_pages_fetched += int(chunk_fetch_stats.get("pages_fetched") or 0)
            filtered_events, filter_stats = _filter_fleet_speeding_events(
                vehicle_events=vehicle_events,
                trip_registrations_norm=trip_registrations_norm,
            )
            rpm_events, rpm_filter_stats = _filter_fleet_rpm_vehicle_events(
                vehicle_events=vehicle_events,
                trip_registrations_norm=trip_registrations_norm,
                trip_vehicle_ids_norm=trip_vehicle_ids_norm,
            )
            fleet_rows_fetched += filter_stats["rows_fetched"]
            fleet_rows_after_registration_filter += filter_stats["rows_kept_after_registration_filter"]
            fleet_rows_after_speed_filter += filter_stats["rows_kept_after_speed_filter"]
            fleet_rows_after_rpm_identity_filter += rpm_filter_stats["rows_kept_after_trip_identity_filter"]
            fleet_provider_labeled_rpm_rows += rpm_filter_stats["provider_labeled_rpm_rows_kept"]
            fleet_chunks_successful = int(chunk_fetch_stats.get("fleet_chunks_successful") or fleet_chunks_successful)
            fallback_chunks_successful = int(
                chunk_fetch_stats.get("fallback_chunks_successful") or fallback_chunks_successful
            )
            fallback_requests += int(chunk_fetch_stats.get("fallback_requests") or 0)
            if chunk_fetch_stats.get("source") == "registration_fallback":
                fallback_events_fetched += int(chunk_fetch_stats.get("records_fetched") or 0)
            elif chunk_fetch_stats.get("source") == "fleet":
                events_fetched_from_fleet += int(chunk_fetch_stats.get("records_fetched") or 0)
            else:
                chunk_records = int(chunk_fetch_stats.get("records_fetched") or 0)
                chunk_fallback_records = int(chunk_fetch_stats.get("fallback_events_fetched") or 0)
                fallback_events_fetched += chunk_fallback_records
                events_fetched_from_fleet += max(0, chunk_records - chunk_fallback_records)
            fallback_subchunks_fetched += int(chunk_fetch_stats.get("fallback_subchunks_fetched") or 0)
            vehicle_events_raw.extend(filtered_events)
            rpm_vehicle_events_raw.extend(rpm_events)

            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Fleet vehicle events adaptive filter stats",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "endpoint": "/vehicles/events",
                    **chunk_fetch_stats,
                    "registrations_count": len(registrations),
                    "limit": vehicle_events_fleet_limit,
                    "max_pages_per_chunk": vehicle_events_fleet_max_pages,
                    "rows_fetched": filter_stats["rows_fetched"],
                    "rows_kept_after_registration_filter": filter_stats["rows_kept_after_registration_filter"],
                    "rows_kept_after_speed_filter": filter_stats["rows_kept_after_speed_filter"],
                    "rows_kept_after_rpm_identity_filter": rpm_filter_stats["rows_kept_after_trip_identity_filter"],
                    "provider_labeled_rpm_rows_kept": rpm_filter_stats["provider_labeled_rpm_rows_kept"],
                },
            )
    else:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Skipping fleet vehicle event fetch because no trip registrations were parsed",
            run_id=run_id,
            context={
                "client_id": client_id,
                "endpoint": "/vehicles/events",
                "registrations_count": 0,
            },
        )

    event_gap_summary = (
        _vehicle_events_source_mismatch_summary(trip_metrics_population_source)
        if not api_owns_trip_metrics
        else _vehicle_events_disabled_summary()
        if event_enrichment_disabled
        else _vehicle_events_gap_summary(vehicle_event_gaps)
    )
    events_fetched_total = events_fetched_from_fleet + fallback_events_fetched
    if vehicle_event_gaps:
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "Vehicle event enrichment is partial; writing gap audit artifact before DB upsert",
            run_id=run_id,
            context={
                "client_id": client_id,
                "phase": "fetch_vehicle_events",
                "endpoint": "/vehicles/events",
                "event_enrichment_mode": event_enrichment_mode,
                "event_fetch_strategy": event_fetch_strategy,
                **event_gap_summary,
                "events_fetched_total": events_fetched_total,
                "events_fetched_from_fleet": events_fetched_from_fleet,
                "events_fetched_from_registration_fallback": fallback_events_fetched,
            },
        )
        artifact_path, artifact_id = _write_vehicle_events_gap_audit_artifact(
            client=client,
            run_id=run_id,
            client_id=client_id,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            mode=event_fetch_strategy,
            summary={
                **event_gap_summary,
                "events_fetched_total": events_fetched_total,
                "events_fetched_from_fleet": events_fetched_from_fleet,
                "events_fetched_from_registration_fallback": fallback_events_fetched,
            },
            gaps=vehicle_event_gaps,
        )
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Vehicle event gap audit artifact uploaded",
            run_id=run_id,
            context={
                "client_id": client_id,
                "artifact_path": artifact_path,
                "artifact_id": artifact_id,
                "artifact_kind": "VEHICLE_EVENTS_GAP_AUDIT",
                **event_gap_summary,
            },
        )

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Fetched and filtered fleet-wide raw vehicle events",
        run_id=run_id,
        context={
            "client_id": client_id,
            "registrations_count": len(registrations),
            "fleet_vehicle_events_fetched": fleet_rows_fetched,
            "fleet_vehicle_events_after_registration_filter": fleet_rows_after_registration_filter,
            "fleet_vehicle_events_after_speed_filter": fleet_rows_after_speed_filter,
            "fleet_vehicle_events_after_rpm_identity_filter": fleet_rows_after_rpm_identity_filter,
            "fleet_provider_labeled_rpm_rows": fleet_provider_labeled_rpm_rows,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_chunks_successful,
            "fallback_requests": fallback_requests,
            "fallback_events_fetched": fallback_events_fetched,
            "fallback_subchunks_fetched": fallback_subchunks_fetched,
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            **event_gap_summary,
            **trip_metrics_source_context,
            "events_fetched_total": events_fetched_total,
            "events_fetched_from_fleet": events_fetched_from_fleet,
            "events_fetched_from_registration_fallback": fallback_events_fetched,
        },
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase end: fleet events fetch",
        run_id=run_id,
        context={
            "client_id": client_id,
            "phase": "fetch_vehicle_events",
            "endpoint": "/vehicles/events",
            "fleet_vehicle_events_fetched": fleet_rows_fetched,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_chunks_successful,
            "fallback_requests": fallback_requests,
            "fallback_events_fetched": fallback_events_fetched,
            "fallback_subchunks_fetched": fallback_subchunks_fetched,
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            **event_gap_summary,
            **trip_metrics_source_context,
            "events_fetched_total": events_fetched_total,
            "events_fetched_from_fleet": events_fetched_from_fleet,
            "events_fetched_from_registration_fallback": fallback_events_fetched,
        },
    )
    vehicle_events_metrics_after = telematics.metrics_snapshot()
    vehicle_events_fetch_elapsed_s = _elapsed_seconds(vehicle_events_fetch_started_at)
    vehicle_events_api_request_elapsed_s = round(
        _provider_metric_delta(
            vehicle_events_metrics_before,
            vehicle_events_metrics_after,
            "request_elapsed_seconds_by_endpoint",
            "/vehicles/events",
        ),
        3,
    )
    vehicle_events_response_parse_elapsed_s = round(
        _provider_metric_delta(
            vehicle_events_metrics_before,
            vehicle_events_metrics_after,
            "response_parse_elapsed_seconds_by_endpoint",
            "/vehicles/events",
        ),
        3,
    )
    vehicle_events_api_requests = _provider_metric_count_delta(
        vehicle_events_metrics_before,
        vehicle_events_metrics_after,
        "/vehicles/events",
    )
    vehicle_events_fallback_api_requests = _provider_metric_count_delta(
        vehicle_events_metrics_before,
        vehicle_events_metrics_after,
        "/vehicles/events:registration",
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: /vehicles/events fetch",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "fetch_vehicle_events",
            "endpoint": "/vehicles/events",
            "elapsed_seconds": vehicle_events_fetch_elapsed_s,
            "api_request_elapsed_seconds": vehicle_events_api_request_elapsed_s,
            "response_parse_elapsed_seconds": vehicle_events_response_parse_elapsed_s,
            "api_requests": vehicle_events_api_requests,
            "registration_fallback_api_requests": vehicle_events_fallback_api_requests,
            "chunks": vehicle_events_chunks_processed,
            "pages_fetched": vehicle_events_pages_fetched,
            "records_fetched": fleet_rows_fetched,
            "limit": vehicle_events_fleet_limit,
            "max_pages_per_chunk": vehicle_events_fleet_max_pages,
            "records_per_second": _records_per_second(fleet_rows_fetched, vehicle_events_fetch_elapsed_s),
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            **event_gap_summary,
        },
    )

    # ---- 3) Trip-level fuel deprecated ----
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Trip-level fuel enrichment is deprecated and skipped. Daily fuel "
        "comes from fuel_daily_aggregation via batch POST /fuel/consumed; "
        "skip_fuel is accepted as a backward-compatible no-op.",
        run_id=run_id,
        context={
            "client_id": client_id,
            "skip_fuel": skip_fuel,
            "trips_total": len(trips_parsed),
            "replacement_job": "jobs.api.telematics.aggregate_trip_fuel_daily",
        },
    )

    # ---- 3a) Compute per-trip speeding violation counts from raw vehicle events ----
    speeding_matching_started_at = time.monotonic()
    bucket_counts, speeding_stats = _compute_speeding_violation_counts(
        trips=trips_for_matching,
        vehicle_events=vehicle_events_raw,
    )
    speeding_matching_elapsed_s = _elapsed_seconds(speeding_matching_started_at)
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Speeding violation matching stats",
        run_id=run_id,
        context={
            "client_id": client_id,
            "registrations_count": len(registrations),
            "vehicle_events_fetched": fleet_rows_fetched,
            "vehicle_events_used_for_speeding": speeding_stats["vehicle_events_total"],
            "speeding_violations_created": speeding_stats["speeding_violations_created"],
            "speeding_violations_matched": speeding_stats["speeding_violations_matched"],
            "speeding_violations_unmatched": speeding_stats["speeding_violations_unmatched"],
            "event_enrichment_mode": event_enrichment_mode,
            **event_gap_summary,
        },
    )

    # ---- 3b) Compute per-trip RPM counts from provider-labeled vehicle events ----
    # RPM/OVERREV source is fleet-wide /vehicles/events labels, not
    # /alerts/notifications and never numeric rpm thresholds. START rows and
    # unsuffixed labels count; END rows are recognized but ignored to avoid
    # double-counting one provider incident.
    rpm_matching_started_at = time.monotonic()
    rpm_counts, rpm_stats = _compute_rpm_vehicle_event_counts(
        trips=trips_for_matching,
        vehicle_events=rpm_vehicle_events_raw,
    )
    rpm_matching_elapsed_s = _elapsed_seconds(rpm_matching_started_at)
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "RPM vehicle-event label matching stats",
        run_id=run_id,
        context={
            "client_id": client_id,
            "endpoint": "/vehicles/events",
            "total_trips": len(trips_for_matching),
            "vehicle_events_fetched": fleet_rows_fetched,
            "provider_labeled_rpm_rows": rpm_stats["rpm_label_rows_seen"],
            "high_rpm_events": rpm_stats["high_rpm_events"],
            "overrev_events": rpm_stats["overrev_events"],
            "high_rpm_label_rows_seen": rpm_stats["high_rpm_label_rows_seen"],
            "overrev_label_rows_seen": rpm_stats["overrev_label_rows_seen"],
            "high_rpm_end_events_ignored": rpm_stats["high_rpm_end_events_ignored"],
            "overrev_end_events_ignored": rpm_stats["overrev_end_events_ignored"],
            "missing_event_ts": rpm_stats["missing_event_ts"],
            "missing_match_identity": rpm_stats["missing_match_identity"],
            "vehicles_with_events": rpm_stats["vehicles_with_events"],
            "registrations_with_events": rpm_stats["registrations_with_events"],
            "matched_high_rpm": rpm_stats["matched_high_rpm"],
            "matched_overrev": rpm_stats["matched_overrev"],
            "trips_with_events": rpm_stats["trips_with_events"],
            "trips_without_events": rpm_stats["trips_without_events"],
            "matches_via_vehicle_id": rpm_stats["matches_via_vehicle_id"],
            "matches_via_registration": rpm_stats["matches_via_registration"],
            "event_enrichment_mode": event_enrichment_mode,
            **event_gap_summary,
        },
    )
    event_enrichment_elapsed_s = _elapsed_seconds(vehicle_events_fetch_started_at)
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: event enrichment and matching",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "event_enrichment",
            "endpoint": "/vehicles/events",
            "elapsed_seconds": event_enrichment_elapsed_s,
            "vehicle_events_fetch_elapsed_seconds": vehicle_events_fetch_elapsed_s,
            "speeding_matching_elapsed_seconds": speeding_matching_elapsed_s,
            "rpm_matching_elapsed_seconds": rpm_matching_elapsed_s,
            "vehicle_events_used_for_speeding": speeding_stats["vehicle_events_total"],
            "vehicle_events_used_for_rpm": rpm_stats["vehicle_events_total"],
            "speeding_violations_created": speeding_stats["speeding_violations_created"],
            "provider_labeled_rpm_rows": rpm_stats["rpm_label_rows_seen"],
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            **event_gap_summary,
        },
    )

    # ---- 4) Upsert into client business DB ----
    schema = _safe_ident(cfg.client_db_schema)
    trips_table = f"{schema}.client_trips"

    db_phase_started_at = time.monotonic()
    db_row_preparation_elapsed_s = 0.0
    db_upsert_elapsed_s = 0.0
    db_trip_upsert_elapsed_s = 0.0
    db_speeding_update_elapsed_s = 0.0
    db_commit_elapsed_s = 0.0
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Phase start: DB upsert",
        run_id=run_id,
        context={
            "client_id": client_id,
            "phase": "db_upsert",
            "event_enrichment_mode": event_enrichment_mode,
            **event_gap_summary,
            **trip_metrics_source_context,
            "trips_prepared": len(trips_parsed),
            "vehicle_events_fetched": fleet_rows_fetched,
        },
    )
    client.log("INFO", "SCRIPT", JOB_SOURCE,
               f"Connecting to client business DB (schema={schema})", run_id=run_id)

    conn = _client_business_pg_conn(cfg)
    recorder.mark_transaction_entered()
    try:
        with conn.cursor() as cur:
            trips_upsert_rows: List[Tuple[Any, ...]] = []
            # Rows whose event-derived metrics this run must not touch.
            trips_preserve_metric_rows: List[Tuple[Any, ...]] = []

            db_row_preparation_started_at = time.monotonic()
            for p in trips_parsed:
                t = p["raw"]
                tid = p["provider_trip_id"]
                distance_m = t.get("trip_distance")

                trip_rid = record_id_mod.for_client_trips(
                    client_id=client_id, provider_trip_id=tid,
                )
                rpm = rpm_counts.get(tid, {"high_rpm": 0, "overrev": 0})
                vehicle_metadata = _vehicle_metadata_for_trip(
                    vehicle_id=t.get("vehicle_id"),
                    registration=p["registration"],
                    by_vehicle_id=vehicle_metadata_by_id,
                    by_registration=vehicle_metadata_by_registration,
                )
                driver_restrictions, _driver_restriction_match_source = _driver_restrictions_for_trip(
                    driver_id=p.get("driver_id"),
                    identification_tag_id=p["identification_tag_id"],
                    driver_name=t.get("driver_name"),
                    driver_surname=t.get("driver_surname"),
                    lookups=driver_restriction_lookups,
                )
                base_row = (
                    client_id,
                    client_code,
                    tid,
                    t.get("vehicle_id"),
                    p["registration"],
                    vehicle_metadata["vehicle_name"],
                    vehicle_metadata["vehicle_description"],
                    t.get("chassis_number"),
                    t.get("driver_name"),
                    t.get("driver_surname"),
                    p["driver_tag_description"],
                    p["identification_tag_id"],
                    driver_restrictions,
                    p["trip_mode"],
                    _parse_provider_dt(t.get("start_timestamp")),
                    t.get("start_location"),
                    p["start_latitude"],
                    p["start_longitude"],
                    t.get("start_geofence_name"),
                    p["start_odometer_value"],
                    _parse_provider_dt(t.get("end_timestamp")),
                    t.get("end_location"),
                    p["end_latitude"],
                    p["end_longitude"],
                    t.get("end_geofence_name"),
                    p["end_odometer_value"],
                    t.get("trip_duration_seconds"),
                    distance_m,
                )
                # M4 first-seen provenance, plus its M-LAG observation instant.
                # Supplied on every row, but the SQL below places BOTH in the
                # INSERT list only and NEVER in `DO UPDATE SET`, so PostgreSQL
                # keeps the original values on an overlapping re-upsert and these
                # are simply discarded. `(None, None)` for a trip with no binding
                # — `strict_meta`, or a row this run did not observe through the
                # compatibility path — which stores the honest NULL meaning
                # "provenance was not captured", never a sentinel and never
                # `synced_at`.
                #
                # They are read from ONE mapping entry, so a row can never carry
                # an identity without an instant or the reverse; the client-side
                # CHECK `ck_client_trips_first_seen_pairing` refuses that shape.
                first_seen_request_id, first_seen_response_received_at = (
                    trips_first_seen.get(tid, (None, None))
                )
                non_metric_tail = (
                    t.get("harsh_braking_events"),
                    t.get("harsh_acceleration_events"),
                    t.get("harsh_cornering_events"),
                    t.get("events_idle"),
                    t.get("idle_time_seconds"),
                    str(trip_rid),
                    synced_at,
                    run_id,
                    first_seen_request_id,
                    first_seen_response_received_at,
                )
                if api_owns_trip_metrics:
                    # Under reconciliation scope this run only bought events for
                    # the trips it discovered, so it is only entitled to speak
                    # about those. For an already-captured trip it has no
                    # observation at all, and `rpm_counts` defaults to zero — the
                    # exact shape that silently overwrote 2,799 enriched FOXTROT
                    # rows with zeros. It writes NULL instead, the column's own
                    # "not enriched" value, and the conflict clause chosen
                    # below keeps that NULL away from the stored row entirely.
                    metrics_owned_by_this_run = (
                        not reconciliation_scope_active or tid in candidate_trip_ids
                    )
                    if metrics_owned_by_this_run:
                        metric_head = (rpm["high_rpm"], rpm["overrev"])
                        metric_speeding = (0, 0, 0)
                    else:
                        metric_head = (None, None)
                        metric_speeding = (0, 0, 0)
                    row = (
                        base_row
                        + metric_head
                        + non_metric_tail[:5]
                        + metric_speeding
                        + non_metric_tail[5:]
                    )
                    if metrics_owned_by_this_run:
                        trips_upsert_rows.append(row)
                    else:
                        trips_preserve_metric_rows.append(row)
                else:
                    trips_upsert_rows.append(base_row + non_metric_tail)

            db_row_preparation_elapsed_s = _elapsed_seconds(db_row_preparation_started_at)
            trip_parse_diagnostics["trips_rows_prepared_for_upsert"] = (
                len(trips_upsert_rows) + len(trips_preserve_metric_rows)
            )

            db_upsert_started_at = time.monotonic()
            if trips_upsert_rows or trips_preserve_metric_rows:
                # On-conflict clause is selected by `overwrite_existing` per the
                # client_dataset_schedule. Metric columns are included only when
                # this API job owns trip metric population for the client.
                metric_insert_columns_sql = ""
                metric_values_sql = ""
                metric_update_sql = ""
                if api_owns_trip_metrics:
                    metric_insert_columns_sql = """
                      high_rpm_events_count,
                      overrev_events_count,
"""
                    metric_values_sql = "%s,%s,"
                    if overwrite_existing:
                        metric_update_sql = """
                      high_rpm_events_count=EXCLUDED.high_rpm_events_count,
                      overrev_events_count=EXCLUDED.overrev_events_count,
                      speeding_140_160_count=EXCLUDED.speeding_140_160_count,
                      speeding_160_170_count=EXCLUDED.speeding_160_170_count,
                      speeding_170_plus_count=EXCLUDED.speeding_170_plus_count,
"""

                if overwrite_existing:
                    # `first_seen_request_id` and
                    # `first_seen_response_received_at_utc` are deliberately
                    # ABSENT from this SET list, exactly as `Dysponent_ID` is
                    # (§1.6). That omission is the whole mechanism: the scheduled
                    # path upserts with `overwrite_existing = true`, so every
                    # overlapping re-request rewrites `synced_at`/`sync_run_id`
                    # — which is precisely why neither can serve as first-seen
                    # provenance. Adding either column here would recreate the
                    # last-touched defect M4 exists to remove, and would make the
                    # M-LAG metric measure the most recent observation instead of
                    # the first one.
                    #
                    # `end_timestamp` IS in this list, and that is correct: the
                    # provider may return a trip that is still open and correct
                    # its end later. The lag is therefore derived from an
                    # immutable minuend and a current subtrahend, which is why it
                    # must never be stored (docs/21 §3).
                    on_conflict_sql = f"""
                    ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
                      client_code=EXCLUDED.client_code,
                      vehicle_id=EXCLUDED.vehicle_id,
                      registration=EXCLUDED.registration,
                      vehicle_name=EXCLUDED.vehicle_name,
                      vehicle_description=EXCLUDED.vehicle_description,
                      chassis_number=EXCLUDED.chassis_number,
                      driver_name=EXCLUDED.driver_name,
                      driver_surname=EXCLUDED.driver_surname,
                      driver_tag_description=EXCLUDED.driver_tag_description,
                      identification_tag_id=EXCLUDED.identification_tag_id,
                      "Driver_Restrictions"=EXCLUDED."Driver_Restrictions",
                      trip_mode=EXCLUDED.trip_mode,
                      start_timestamp=EXCLUDED.start_timestamp,
                      start_location=EXCLUDED.start_location,
                      start_latitude=EXCLUDED.start_latitude,
                      start_longitude=EXCLUDED.start_longitude,
                      start_geofence_name=EXCLUDED.start_geofence_name,
                      start_odometer_value=EXCLUDED.start_odometer_value,
                      end_timestamp=EXCLUDED.end_timestamp,
                      end_location=EXCLUDED.end_location,
                      end_latitude=EXCLUDED.end_latitude,
                      end_longitude=EXCLUDED.end_longitude,
                      end_geofence_name=EXCLUDED.end_geofence_name,
                      end_odometer_value=EXCLUDED.end_odometer_value,
                      trip_duration_seconds=EXCLUDED.trip_duration_seconds,
                      trip_distance_meters=EXCLUDED.trip_distance_meters,
{metric_update_sql}                      harsh_braking_events=EXCLUDED.harsh_braking_events,
                      harsh_acceleration_events=EXCLUDED.harsh_acceleration_events,
                      harsh_turning_events=EXCLUDED.harsh_turning_events,
                      idle_events=EXCLUDED.idle_events,
                      idle_time_seconds=EXCLUDED.idle_time_seconds,
                      record_id=EXCLUDED.record_id,
                      sync_run_id=EXCLUDED.sync_run_id,
                      synced_at=EXCLUDED.synced_at
                    """
                else:
                    on_conflict_sql = "ON CONFLICT (client_id, provider_trip_id) DO NOTHING"
                # Same clause minus the five event-derived metric assignments.
                # With `overwrite_existing=False` the clause is already
                # DO NOTHING and there is nothing to strip.
                preserve_on_conflict_sql = (
                    on_conflict_sql.replace(metric_update_sql, "")
                    if metric_update_sql else on_conflict_sql
                )

                speeding_insert_columns_sql = """
                      speeding_140_160_count,
                      speeding_160_170_count,
                      speeding_170_plus_count,
""" if api_owns_trip_metrics else ""
                speeding_values_sql = "%s,%s,%s," if api_owns_trip_metrics else ""
                db_trip_upsert_started_at = time.monotonic()
                trip_insert_sql = f"""
                    INSERT INTO {trips_table} (
                      client_id,
                      client_code,
                      provider_trip_id,
                      vehicle_id,
                      registration,
                      vehicle_name,
                      vehicle_description,
                      chassis_number,
                      driver_name,
                      driver_surname,
                      driver_tag_description,
                      identification_tag_id,
                      "Driver_Restrictions",
                      trip_mode,
                      start_timestamp,
                      start_location,
                      start_latitude,
                      start_longitude,
                      start_geofence_name,
                      start_odometer_value,
                      end_timestamp,
                      end_location,
                      end_latitude,
                      end_longitude,
                      end_geofence_name,
                      end_odometer_value,
                      trip_duration_seconds,
                      trip_distance_meters,
{metric_insert_columns_sql}                      harsh_braking_events,
                      harsh_acceleration_events,
                      harsh_turning_events,
                      idle_events,
                      idle_time_seconds,
{speeding_insert_columns_sql}                      record_id,
                      synced_at,
                      sync_run_id,
                      first_seen_request_id,
                      first_seen_response_received_at_utc
                    )
                    VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{metric_values_sql}%s,%s,%s,%s,%s,{speeding_values_sql}%s,%s,%s,%s,%s
                    )
                    """
                # One INSERT body, two conflict resolutions. Both batches carry
                # the identical column list and parameter shape, so a row can
                # never be bound against the wrong statement.
                #
                # Both statements are issued through
                # `client_trips_admission.execute_client_trips_insert`, which is
                # the only sanctioned way this repository writes `client_trips`.
                # It applies the 2,000 km cap to the exact value each row binds
                # to `trip_distance_meters`, inside the call that issues the
                # statement, so no edit between parsing and here can reintroduce
                # a row the invariant forbids. The parse gate above already
                # removed them, so `rejected` is expected to stay 0.
                upserted_rowcount = 0
                if trips_upsert_rows:
                    upsert_result = client_trips_admission.execute_client_trips_insert(
                        cur,
                        sql=trip_insert_sql + on_conflict_sql,
                        rows=trips_upsert_rows,
                        distance_index=CLIENT_TRIPS_DISTANCE_VALUE_INDEX,
                        provider_trip_id_index=CLIENT_TRIPS_PROVIDER_TRIP_ID_VALUE_INDEX,
                        gate=trips_admission,
                    )
                    upserted_rowcount += upsert_result.rowcount
                    trip_parse_diagnostics[
                        "client_trips_rejected_distance_over_2000km_at_persistence"
                    ] += upsert_result.rejected
                if trips_preserve_metric_rows:
                    preserve_result = client_trips_admission.execute_client_trips_insert(
                        cur,
                        sql=trip_insert_sql + preserve_on_conflict_sql,
                        rows=trips_preserve_metric_rows,
                        distance_index=CLIENT_TRIPS_DISTANCE_VALUE_INDEX,
                        provider_trip_id_index=CLIENT_TRIPS_PROVIDER_TRIP_ID_VALUE_INDEX,
                        gate=trips_admission,
                    )
                    upserted_rowcount += preserve_result.rowcount
                    trip_parse_diagnostics[
                        "client_trips_rejected_distance_over_2000km_at_persistence"
                    ] += preserve_result.rejected
                db_trip_upsert_elapsed_s = _elapsed_seconds(db_trip_upsert_started_at)
                trip_parse_diagnostics["trips_rows_upserted"] = upserted_rowcount
            else:
                trip_parse_diagnostics["trips_rows_upserted"] = 0

            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Provider trip upsert diagnostics",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "overwrite_existing": overwrite_existing,
                    "trips_rows_prepared_for_upsert": trip_parse_diagnostics["trips_rows_prepared_for_upsert"],
                    "trips_rows_upserted": trip_parse_diagnostics["trips_rows_upserted"],
                },
            )

            # ---- 5) Write raw-event speeding violation counts back ----
            trip_bucket_update_rows: List[Tuple[Any, ...]] = []
            if not api_owns_trip_metrics:
                client.log(
                    "INFO", "SCRIPT", JOB_SOURCE,
                    "Speeding bucket update skipped by trip_metrics_population_source",
                    run_id=run_id,
                    context={
                        "client_id": client_id,
                        "phase": "db_upsert",
                        **trip_metrics_source_context,
                        **trip_metrics_skip_context,
                    },
                )
            else:
                for t in trips_for_matching:
                    provider_trip_id = int(t["provider_trip_id"])
                    if (
                        reconciliation_scope_active
                        and provider_trip_id not in candidate_trip_ids
                    ):
                        # No events were bought for this trip, so `bucket_counts`
                        # holds a default zero rather than an observation. The
                        # speeding columns are NOT NULL, so unlike the RPM pair
                        # they cannot express "unknown" — the only way not to lie
                        # about them is not to write them.
                        continue
                    bc = bucket_counts[provider_trip_id]
                    trip_bucket_update_rows.append((
                        bc["speeding_140_160_count"],
                        bc["speeding_160_170_count"],
                        bc["speeding_170_plus_count"],
                        run_id,
                        synced_at,
                        client_id,
                        provider_trip_id,
                        run_id,
                    ))

            if trip_bucket_update_rows:
                # When overwrite_existing=False, the upsert above used DO NOTHING,
                # so already-existing trips kept their prior `sync_run_id`. The
                # `AND sync_run_id = %s` filter scopes this UPDATE to rows we
                # actually inserted in this run, leaving prior data untouched.
                # When overwrite_existing=True, every upserted row carries the
                # current run_id, so the same filter still matches the full
                # working set without changes.
                db_speeding_update_started_at = time.monotonic()
                cur.executemany(
                    f"""
                    UPDATE {trips_table}
                    SET
                      speeding_140_160_count=%s,
                      speeding_160_170_count=%s,
                      speeding_170_plus_count=%s,
                      sync_run_id=%s,
                      synced_at=%s
                    WHERE client_id=%s
                      AND provider_trip_id=%s
                      AND sync_run_id=%s
                    """,
                    trip_bucket_update_rows,
                )
                db_speeding_update_elapsed_s = _elapsed_seconds(db_speeding_update_started_at)

            db_upsert_elapsed_s = _elapsed_seconds(db_upsert_started_at)
            db_commit_started_at = time.monotonic()
            conn.commit()
            db_commit_elapsed_s = _elapsed_seconds(db_commit_started_at)
            recorder.mark_transaction_committed()
            recorder.record_counts(
                prepared=int(
                    trip_parse_diagnostics["trips_rows_prepared_for_upsert"] or 0
                ),
                upserted=int(trip_parse_diagnostics["trips_rows_upserted"] or 0),
                malformed=(
                    int(trip_parse_diagnostics["trips_rows_malformed_trip_id"] or 0)
                    + int(
                        trip_parse_diagnostics["trips_rows_malformed_timestamp"] or 0
                    )
                    + int(
                        trip_parse_diagnostics["trips_rows_other_parse_error"] or 0
                    )
                ),
            )
            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Phase end: DB upsert",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "phase": "db_upsert",
                    "event_enrichment_mode": event_enrichment_mode,
                    "event_fetch_strategy": event_fetch_strategy,
                    **event_gap_summary,
                    **trip_metrics_source_context,
                    "trips_rows_upserted": trip_parse_diagnostics["trips_rows_upserted"],
                    "speeding_bucket_update_rows": len(trip_bucket_update_rows),
                },
            )
            db_phase_elapsed_s = _elapsed_seconds(db_phase_started_at)
            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Performance: DB upsert",
                run_id=run_id,
                context={
                    **perf_base_context,
                    "phase": "db_upsert",
                    "elapsed_seconds": db_phase_elapsed_s,
                    "db_row_preparation_elapsed_seconds": db_row_preparation_elapsed_s,
                    "db_upsert_elapsed_seconds": db_upsert_elapsed_s,
                    "db_trip_upsert_elapsed_seconds": db_trip_upsert_elapsed_s,
                    "db_speeding_update_elapsed_seconds": db_speeding_update_elapsed_s,
                    "db_commit_elapsed_seconds": db_commit_elapsed_s,
                    "trips_rows_prepared_for_upsert": len(trips_upsert_rows),
                    "trips_rows_upserted": trip_parse_diagnostics["trips_rows_upserted"],
                    "speeding_bucket_update_rows": len(trip_bucket_update_rows),
                    "rows_per_second": _records_per_second(len(trips_upsert_rows), db_phase_elapsed_s),
                    "overwrite_existing": overwrite_existing,
                    **trip_metrics_source_context,
                },
            )

    finally:
        conn.close()

    # ---- 6) Summary ----
    total_trips = len(trips_raw)
    if not api_owns_trip_metrics:
        completion_message = (
            "Run completed: trips upserted; event-derived trip metrics skipped by "
            "trip_metrics_population_source; locations written; trip-level fuel skipped."
        )
    elif event_enrichment_disabled:
        completion_message = (
            "Run completed: trips upserted; vehicle event enrichment disabled; "
            "OVERREV and speeding bucket event-derived counts set to zero; "
            "locations written; trip-level fuel skipped."
        )
    else:
        completion_message = (
            "Run completed: trips upserted; raw-event speeding and provider-labeled "
            "RPM/OVERREV counts computed; locations written; trip-level fuel skipped."
        )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        completion_message,
        run_id=run_id,
        context={
            "client_id": client_id,
            "window_start_ts": str(window_start_ts),
            "window_end_ts": str(window_end_ts),
            "trips_fetched": total_trips,
            "trips_chunk_days": trips_chunk_days,
            "trips_chunks": len(trips_chunk_summaries),
            "trips_pagination_mode": trips_pagination_mode,
            **trip_parse_diagnostics,
            "fleet_vehicle_events_fetched": fleet_rows_fetched,
            "fleet_vehicle_events_after_registration_filter": fleet_rows_after_registration_filter,
            "fleet_vehicle_events_after_speed_filter": fleet_rows_after_speed_filter,
            "fleet_vehicle_events_after_rpm_identity_filter": fleet_rows_after_rpm_identity_filter,
            "fleet_provider_labeled_rpm_rows": fleet_provider_labeled_rpm_rows,
            "fleet_chunks_successful": fleet_chunks_successful,
            "fallback_chunks_successful": fallback_chunks_successful,
            "fallback_requests": fallback_requests,
            "fallback_events_fetched": fallback_events_fetched,
            "fallback_subchunks_fetched": fallback_subchunks_fetched,
            "vehicle_inventory_rows": len(vehicle_inventory_rows),
            **vehicle_metadata_stats,
            "driver_inventory_rows": len(driver_inventory_rows),
            **driver_restriction_stats,
            "high_rpm_events": rpm_stats["high_rpm_events"],
            "overrev_events": rpm_stats["overrev_events"],
            "high_rpm_end_events_ignored": rpm_stats["high_rpm_end_events_ignored"],
            "overrev_end_events_ignored": rpm_stats["overrev_end_events_ignored"],
            "matched_high_rpm": rpm_stats["matched_high_rpm"],
            "matched_overrev": rpm_stats["matched_overrev"],
            "speeding_violations_created": speeding_stats["speeding_violations_created"],
            "speeding_violations_matched": speeding_stats["speeding_violations_matched"],
            "speeding_violations_unmatched": speeding_stats["speeding_violations_unmatched"],
            "trip_level_fuel_enrichment": "deprecated_skipped",
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
            **event_gap_summary,
            **trip_metrics_source_context,
            "events_fetched_total": events_fetched_total,
            "events_fetched_from_fleet": events_fetched_from_fleet,
            "events_fetched_from_registration_fallback": fallback_events_fetched,
        },
    )
    provider_metrics_at_job_end = telematics.metrics_snapshot()
    total_job_elapsed_s = _elapsed_seconds(job_started_at)
    total_provider_requests = int(provider_metrics_at_job_end.get("total_requests", 0)) - int(
        provider_metrics_at_job_start.get("total_requests", 0)
    )
    total_pages_fetched = (
        trips_pages_fetched
        + vehicles_pages_fetched
        + drivers_pages_fetched
        + vehicle_events_pages_fetched
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Performance: total job runtime",
        run_id=run_id,
        context={
            **perf_base_context,
            "phase": "total",
            "elapsed_seconds": total_job_elapsed_s,
            "trips_fetch_elapsed_seconds": trips_fetch_elapsed_s,
            "vehicles_fetch_elapsed_seconds": vehicles_fetch_elapsed_s,
            "drivers_fetch_elapsed_seconds": drivers_fetch_elapsed_s,
            "vehicle_events_fetch_elapsed_seconds": vehicle_events_fetch_elapsed_s,
            "trip_parse_elapsed_seconds": trip_parse_elapsed_s,
            "event_enrichment_elapsed_seconds": event_enrichment_elapsed_s,
            "db_row_preparation_elapsed_seconds": db_row_preparation_elapsed_s,
            "db_upsert_elapsed_seconds": db_upsert_elapsed_s,
            "db_commit_elapsed_seconds": db_commit_elapsed_s,
            "total_api_requests": total_provider_requests,
            "api_requests_by_endpoint": provider_metrics_at_job_end.get("request_count_by_endpoint", {}),
            **trip_metrics_source_context,
            "api_request_elapsed_seconds_by_endpoint": {
                key: round(float(value), 3)
                for key, value in (
                    provider_metrics_at_job_end.get("request_elapsed_seconds_by_endpoint") or {}
                ).items()
            },
            "response_parse_elapsed_seconds_by_endpoint": {
                key: round(float(value), 3)
                for key, value in (
                    provider_metrics_at_job_end.get("response_parse_elapsed_seconds_by_endpoint") or {}
                ).items()
            },
            "total_pages_fetched": total_pages_fetched,
            "trips_pages_fetched": trips_pages_fetched,
            "trips_chunk_days": trips_chunk_days,
            "trips_chunks": len(trips_chunk_summaries),
            "vehicles_pages_fetched": vehicles_pages_fetched,
            "drivers_pages_fetched": drivers_pages_fetched,
            "vehicle_events_pages_fetched": vehicle_events_pages_fetched,
            "vehicle_events_chunks": vehicle_events_chunks_processed,
            "trips_fetched": total_trips,
            "vehicle_inventory_rows": len(vehicle_inventory_rows),
            "driver_inventory_rows": len(driver_inventory_rows),
            "vehicle_events_records_fetched": fleet_rows_fetched,
            "trips_rows_prepared_for_upsert": trip_parse_diagnostics["trips_rows_prepared_for_upsert"],
            "trips_rows_upserted": trip_parse_diagnostics["trips_rows_upserted"],
            "trips_records_per_second": _records_per_second(total_trips, trips_fetch_elapsed_s),
            "vehicle_events_records_per_second": _records_per_second(
                fleet_rows_fetched,
                vehicle_events_fetch_elapsed_s,
            ),
            "db_rows_per_second": _records_per_second(
                trip_parse_diagnostics["trips_rows_prepared_for_upsert"],
                db_upsert_elapsed_s + db_commit_elapsed_s,
            ),
            "event_enrichment_mode": event_enrichment_mode,
            "event_fetch_strategy": event_fetch_strategy,
        },
    )


def run(client, run_id: str, params: dict):
    """Runner entry point. Owns the single terminal execution record.

    The whole job body lives in `_run`. This wrapper exists so that *every* exit
    path — normal completion, the disabled-schedule skip, a provider safety
    stop, an uncaught exception — passes through one place that states what the
    run actually did.

    The recorder is enabled when `TELEMATICS_TRIPS_EXECUTION_OUTCOME_FILE` is
    present. The recovery flow supplies it, and since M3 so does a compatibility
    `trips_sync` scheduled fire — for those the record is what the dispatcher's
    coverage gate reads. A `strict_meta` fire and an ordinary manual invocation
    supply nothing, so there the recorder stays inert and behavior is unchanged.
    The variable itself is not an authentication mechanism and not proof of
    launcher provenance; only the record's contents are verified.

    Exceptions are re-raised untouched, so `run_context` still marks the
    platform run `FAILED` exactly as before.
    """
    recorder = ExecutionOutcomeRecorder(params=params, dataset_name=DATASET_NAME)
    try:
        _run(client, run_id, params, recorder)
    except BaseException:
        recorder.record_failed()
        raise
    recorder.record_executed()
