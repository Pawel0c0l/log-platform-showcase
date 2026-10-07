from __future__ import annotations

TRIPS_STABILIZATION_DELAY_SECONDS_DEFAULT = 10_800
TRIPS_STABILIZATION_DELAY_SECONDS_MIN = 0
TRIPS_OVERLAP_SECONDS_DEFAULT = 3_600
TRIPS_OVERLAP_SECONDS_MIN = 0
TRIPS_MAX_RECOVERY_SPAN_SECONDS_DEFAULT = 2_678_400
TRIPS_MAX_RECOVERY_SPAN_SECONDS_MIN = 1

# M7 (docs/20 §3.1a, §3.3, §14) raises this ceiling from 2_678_400 (31 d) to
# 32 d + O, the exact threshold at which the `R` clamp stops binding for the
# approved rolling monthly cadence.
#
# WHY THE OLD CEILING WAS WRONG FOR `R`.
#   31 days is the provider's documented limit on a single `/trips` lookup
#   period, and migration 056 adopted it as the `R` ceiling on that basis. The
#   two are different quantities. The provider limit binds each *request*, and
#   `_build_trip_fetch_chunks` already keeps every request to `chunk_days`
#   (default 2, cap 5) — the effective window never reaches the provider as one
#   lookup. `R` bounds the *derived window*, so tying it to a per-request
#   ceiling capped the reconciliation horizon for no provider-side reason.
#
# WHY THIS EXACT VALUE.
#   `derive_effective_window` clamps `E_start = max(base_start, E_end − R)`, so
#   the clamp binds exactly when `R < L·86400 + O`. For the approved monthly
#   `L = 32` with `O = 3_600` that threshold is 32·86400 + 3600 = 2_768_400.
#   At exactly this value `base_start == E_end − R`, so the clamp is inert and
#   the monthly window keeps its overlap pre-roll instead of silently losing an
#   hour of it. A larger ceiling would buy nothing any approved cadence uses.
TRIPS_MAX_RECOVERY_SPAN_SECONDS_MAX = 2_768_400
CLOSED_INTERVAL_GRID_STEP_SECONDS = 1


def _validate_integer(
    *, field_name: str, value: object, minimum: int, maximum: int | None = None
) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer; got {value!r}")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}; got {value!r}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}; got {value!r}")
    return value


def validate_trips_stabilization_config(
    *,
    stabilization_delay_seconds: object,
    overlap_seconds: object,
    max_recovery_span_seconds: object,
) -> tuple[int, int, int]:
    """Validate the inert client-account D/O/R configuration contract."""
    delay = _validate_integer(
        field_name="trips_stabilization_delay_seconds",
        value=stabilization_delay_seconds,
        minimum=TRIPS_STABILIZATION_DELAY_SECONDS_MIN,
    )
    overlap = _validate_integer(
        field_name="trips_overlap_seconds",
        value=overlap_seconds,
        minimum=TRIPS_OVERLAP_SECONDS_MIN,
    )
    recovery = _validate_integer(
        field_name="trips_max_recovery_span_seconds",
        value=max_recovery_span_seconds,
        minimum=TRIPS_MAX_RECOVERY_SPAN_SECONDS_MIN,
        maximum=TRIPS_MAX_RECOVERY_SPAN_SECONDS_MAX,
    )
    if overlap > recovery:
        raise ValueError(
            "trips_overlap_seconds must not exceed "
            "trips_max_recovery_span_seconds"
        )
    return delay, overlap, recovery


def validate_trips_schedule_coupling(
    *,
    schedule_interval_seconds: object,
    maximum_dst_extension_seconds: object,
    lookback_duration_seconds: object,
    overlap_seconds: object,
    max_recovery_span_seconds: object,
) -> int:
    """Validate continuity from supplied durations, without clock/timezone access."""
    schedule_interval = _validate_integer(
        field_name="schedule_interval_seconds",
        value=schedule_interval_seconds,
        minimum=1,
    )
    dst_extension = _validate_integer(
        field_name="maximum_dst_extension_seconds",
        value=maximum_dst_extension_seconds,
        minimum=0,
    )
    lookback = _validate_integer(
        field_name="lookback_duration_seconds",
        value=lookback_duration_seconds,
        minimum=1,
    )
    overlap = _validate_integer(
        field_name="trips_overlap_seconds",
        value=overlap_seconds,
        minimum=TRIPS_OVERLAP_SECONDS_MIN,
    )
    recovery = _validate_integer(
        field_name="trips_max_recovery_span_seconds",
        value=max_recovery_span_seconds,
        minimum=TRIPS_MAX_RECOVERY_SPAN_SECONDS_MIN,
        maximum=TRIPS_MAX_RECOVERY_SPAN_SECONDS_MAX,
    )
    if overlap > recovery:
        raise ValueError(
            "trips_overlap_seconds must not exceed "
            "trips_max_recovery_span_seconds"
        )

    delta_max = schedule_interval + dst_extension
    continuity_limit = lookback + overlap + CLOSED_INTERVAL_GRID_STEP_SECONDS
    if delta_max > continuity_limit:
        raise ValueError(
            "schedule coupling violates closed-interval continuity: "
            f"delta_max={delta_max} exceeds lookback + overlap + "
            f"{CLOSED_INTERVAL_GRID_STEP_SECONDS}={continuity_limit}"
        )
    return delta_max
