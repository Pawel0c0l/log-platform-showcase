from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import Timeout as RequestsTimeout

from jobs.api.telematics.provider_safety import (
    TelematicsProviderSafetyError,
    CompatibilitySafetyLimits,
    LogFn,
    PAGINATION_COMPAT_DUPLICATE_IN_PAGE,
    PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED,
    PAGINATION_COMPAT_IDENTITY_MISSING,
    PAGINATION_COMPAT_PAGE_OVERLAP,
    PAGINATION_COMPAT_PAGE_REPEATED,
    PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED,
    PAGINATION_COMPAT_ROWS_EXCEED_LIMIT,
    PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED,
    PAGINATION_COMPAT_SHAPE_UNSTABLE,
    PAGINATION_COMPAT_TOTAL_EXCEEDED,
    PAGINATION_COMPAT_TOTAL_INVALID,
    PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED,
    PAGINATION_COMPAT_TOTAL_UNSTABLE,
    ProviderRunBudget,
    SafetyLimits,
)
from jobs.trips_pagination_mode import (
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    normalize_trips_pagination_mode,
)


TELEMATICS_PROVIDER_PAGE_LIMIT_ENV = "TELEMATICS_PROVIDER_PAGE_LIMIT"
TELEMATICS_PROVIDER_DEFAULT_PAGE_LIMIT = 1000


def provider_page_limit_from_env() -> int:
    return int(os.getenv(TELEMATICS_PROVIDER_PAGE_LIMIT_ENV, str(TELEMATICS_PROVIDER_DEFAULT_PAGE_LIMIT)))


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iter_31d_windows(
    window_start_ts: datetime,
    window_end_ts: datetime,
    *,
    max_days: int = 31,
    overlap_seconds: int = 1,
) -> Iterable[Tuple[datetime, datetime]]:
    """
    Split an execution window into provider-legal sub-windows.

    The Telematics API docs state a maximum 31 days lookup period for `start_timestamp/end_timestamp`
    and `date_from/date_to`. We split into <=31-day chunks and overlap by a second to avoid
    missing events that fall exactly on boundaries.
    """
    start_ts = _to_utc(window_start_ts)
    end_ts = _to_utc(window_end_ts)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")

    current_start = start_ts
    max_delta = timedelta(days=max_days)
    while True:
        current_end = min(end_ts, current_start + max_delta)
        yield current_start, current_end

        if current_end >= end_ts:
            return

        next_start = current_end - timedelta(seconds=overlap_seconds)
        if next_start <= current_start:
            next_start = current_start + timedelta(seconds=1)
        current_start = next_start


def iter_24h_windows(
    window_start_ts: datetime,
    window_end_ts: datetime,
) -> Iterable[Tuple[datetime, datetime]]:
    """Split an execution window into <=24h chunks for raw vehicle events."""
    start_ts = _to_utc(window_start_ts)
    end_ts = _to_utc(window_end_ts)
    if end_ts < start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")

    current_start = start_ts
    max_delta = timedelta(hours=24)
    while True:
        current_end = min(end_ts, current_start + max_delta)
        yield current_start, current_end

        if current_end >= end_ts:
            return

        if current_end <= current_start:
            raise ValueError("vehicle event window splitter made no progress")
        current_start = current_end


def _provider_dt_str(dt: datetime) -> str:
    dtu = _to_utc(dt)
    return dtu.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Warsaw wall-clock request wire-time contract.
#
# Two Telematics endpoints have been directly measured against the live provider
# and both interpret their request timestamps as **Europe/Warsaw local
# wall-clock** while returning **UTC** row timestamps:
#
#   * `/trips` — live GET-only probes against ALPHA00001 on 2026-08-10 (four
#     requests, two closed July windows, six pre-registered reference trips).
#     Sending the UTC projection of a Warsaw window returned rows offset by
#     exactly the Warsaw UTC offset; sending the Warsaw wall-clock returned the
#     intended rows, and an independent control of 1,487 other-vehicle rows
#     matched already-stored `client_trips` rows at 97-99%.
#
#   * `/vehicles/events` — live GET-only probes against DELTA00001 on
#     2026-08-10, registration EL5JV96, provider trip 432316079
#     (2026-06-29 08:00:32Z - 08:43:57Z), probe interval 07:58:32Z - 08:45:57Z.
#     Variant A (UTC serialization, wire `07:58:32` - `08:45:57`) returned 14
#     rows, all at 06:43:54 - 06:45:57 UTC — classified `minus_offset`, zero
#     rows inside the intended interval. Variant B (Warsaw wall-clock, wire
#     `09:58:32` - `10:45:57`) returned a first page of 100 rows all inside the
#     intended interval, 19 of them of the pre-registered expected evidence
#     type, with the registration filter honored.
#
# The response side needs no change for either endpoint:
# `_parse_provider_dt` / `_parse_provider_dt_optional` already read a naive row
# timestamp as UTC. 180,780 stored July trips reconcile to the second against
# the independent D105.2 report under that reading, and every Variant B event
# row landed in the intended absolute interval under it.
#
# This asymmetry is an empirically observed provider behaviour, not a
# contractual guarantee, and it is applied **per endpoint that has been
# measured** — never globally. Every other Telematics endpoint keeps
# `_provider_dt_str` until its own contract is independently proven; see
# docs/18.
# ---------------------------------------------------------------------------

PROVIDER_WALL_CLOCK_TIMEZONE_NAME = "Europe/Warsaw"

# Endpoint-scoped aliases. They are deliberately separate names so that a
# future divergence (one endpoint changing, or a third endpoint being measured)
# is a one-line change rather than a global one.
TRIPS_REQUEST_TIMEZONE_NAME = PROVIDER_WALL_CLOCK_TIMEZONE_NAME
VEHICLE_EVENTS_REQUEST_TIMEZONE_NAME = PROVIDER_WALL_CLOCK_TIMEZONE_NAME

# `iter_31d_windows` splits `/trips` execution windows at 30 days rather than
# the provider's documented 31-day maximum. The DST widening below may extend a
# sub-window by up to the maximum Warsaw UTC-offset change (1 hour) on each
# side, and 30 days + 2 hours still fits inside the provider limit.
TRIPS_MAX_SUB_WINDOW_DAYS = 30

PROVIDER_WIRE_DT_FORMAT = "%Y-%m-%d %H:%M:%S"
TRIPS_WIRE_DT_FORMAT = PROVIDER_WIRE_DT_FORMAT

# `/vehicles/events` rejects a request window of 24 hours or more. The limit is
# enforced on the *effective* (post-widening) absolute span and on the wall-
# clock span actually written on the wire, because a nominal window safely
# under the limit can widen across a DST transition.
VEHICLE_EVENTS_MAX_REQUEST_WINDOW = timedelta(hours=24)

# Largest intended `/vehicles/events` window a caller may ask for. Worst-case
# DST widening is one Warsaw offset change (1 hour) per endpoint, so an
# intended window at this bound can never reach `VEHICLE_EVENTS_MAX_REQUEST_
# WINDOW` on either measure. Callers chunk far below it (default 4 h); this
# exists so that a misconfiguration is split before conversion rather than
# rejected by the provider mid-run.
VEHICLE_EVENTS_MAX_INTENDED_WINDOW = timedelta(hours=21)

# One widening pass moves an ambiguous endpoint out of the repeated hour, so a
# second pass can only confirm it. The bound exists to make non-termination
# impossible rather than because a second pass is expected to do work.
_WIRE_WIDENING_PASSES = 2


def _provider_wall_clock_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo(PROVIDER_WALL_CLOCK_TIMEZONE_NAME)


# Retained name: `/trips` is the endpoint this primitive was first proven for.
_trips_request_tz = _provider_wall_clock_tz


def _wall_clock_wire_dt_str(dt: datetime) -> str:
    """Serialize one instant as the Warsaw wall-clock the provider expects.

    Input is always an absolute instant (naive values are read as UTC by
    `_to_utc`, matching the rest of this module), so the conversion cannot pick
    up the host's local timezone and cannot be applied twice: the output is a
    plain string, never a datetime that could be converted again.
    """
    return _to_utc(dt).astimezone(_provider_wall_clock_tz()).strftime(PROVIDER_WIRE_DT_FORMAT)


_trips_wire_dt_str = _wall_clock_wire_dt_str


def _wall_clock_of(dt_utc: datetime, tz) -> datetime:
    """The naive Warsaw wall-clock `_wall_clock_wire_dt_str` would emit for `dt_utc`."""
    return dt_utc.astimezone(tz).replace(tzinfo=None, microsecond=0)


def _wall_clock_resolutions(wall: datetime, tz) -> List[datetime]:
    """Every UTC instant the provider could mean by one Warsaw wall-clock value.

    Two instants inside the autumn fold, one otherwise. Never empty for a value
    produced by `_wall_clock_of`, because `astimezone` cannot emit a local time
    inside the spring gap.
    """
    resolutions: List[datetime] = []
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(tz).replace(tzinfo=None, microsecond=0) != wall:
            continue
        if candidate not in resolutions:
            resolutions.append(candidate)
    return sorted(resolutions)


def _widen_start_for_ambiguity(start_utc: datetime, tz) -> datetime:
    """Move the start back until its *latest* reading is at or before `start_utc`."""
    effective = start_utc
    for _ in range(_WIRE_WIDENING_PASSES):
        resolutions = _wall_clock_resolutions(_wall_clock_of(effective, tz), tz)
        if not resolutions or resolutions[-1] <= start_utc:
            return effective
        effective -= resolutions[-1] - start_utc
    return effective


def _widen_end_for_ambiguity(end_utc: datetime, tz) -> datetime:
    """Move the end forward until its *earliest* reading is at or after `end_utc`."""
    effective = end_utc
    for _ in range(_WIRE_WIDENING_PASSES):
        resolutions = _wall_clock_resolutions(_wall_clock_of(effective, tz), tz)
        if not resolutions or resolutions[0] >= end_utc:
            return effective
        effective += end_utc - resolutions[0]
    return effective


# Retained names used by the `/trips` regression suite.
_trips_wall_clock = _wall_clock_of
_trips_wall_clock_resolutions = _wall_clock_resolutions
_trips_widen_start = _widen_start_for_ambiguity
_trips_widen_end = _widen_end_for_ambiguity


def _wall_clock_wire_window(
    start: datetime, end: datetime,
) -> Tuple[str, str, datetime, datetime, Dict[str, Any]]:
    """Convert an absolute interval into a Warsaw wall-clock request window.

    Returns `(start_str, end_str, effective_start_utc, effective_end_utc,
    diagnostics)`. This is the endpoint-neutral primitive; endpoint wrappers
    add their own limits and diagnostics.

    Because the provider addresses records by wall-clock only, a window near a
    Europe/Warsaw DST transition cannot always be expressed unambiguously:

      * **Autumn fallback.** Local 02:00-02:59:59 occurs twice, so one wire
        string denotes two instants an hour apart and we cannot control which
        one the provider picks. A window contained in the repeated hour even
        serializes to an inverted or empty wall-clock interval.
      * **Spring forward.** Local 02:00-02:59:59 does not exist. `astimezone`
        never emits a time inside the gap, and local time advances
        monotonically across it, so a window that merely spans the gap is
        already exact and needs no correction.

    The enforced invariant is therefore stated on the *worst-case* reading, not
    on the offsets of the two endpoints::

        max(readings(start_str)) <= start   and   min(readings(end_str)) >= end

    Each endpoint is widened only as far as its own ambiguity requires, which
    is at most one Warsaw offset change (1 hour) per side. Widening on the
    offset delta alone is **not** sufficient: a window lying wholly inside one
    occurrence of the repeated hour has equal offsets at both endpoints and is
    ambiguous regardless, and leaving it unwidened can omit the entire intended
    interval.

    The result can only over-fetch, never under-fetch. Losing an absolute
    interval is not recoverable the way a duplicate read is, which is why the
    asymmetry is deliberate.

    Sub-second inputs are floored at the start and ceiled at the end, because
    the wire format is second-precision and truncating an end bound downwards
    would shrink the requested interval.
    """
    start_utc = _to_utc(start).replace(microsecond=0)
    end_utc = _to_utc(end)
    if end_utc.microsecond:
        end_utc = end_utc.replace(microsecond=0) + timedelta(seconds=1)

    tz = _provider_wall_clock_tz()
    effective_start = _widen_start_for_ambiguity(start_utc, tz)
    effective_end = _widen_end_for_ambiguity(end_utc, tz)

    widened_start_seconds = int((start_utc - effective_start).total_seconds())
    widened_end_seconds = int((effective_end - end_utc).total_seconds())

    return (
        _wall_clock_wire_dt_str(effective_start),
        _wall_clock_wire_dt_str(effective_end),
        effective_start,
        effective_end,
        {
            "requested_window_start_utc": start_utc.isoformat(),
            "requested_window_end_utc": end_utc.isoformat(),
            "effective_window_start_utc": effective_start.isoformat(),
            "effective_window_end_utc": effective_end.isoformat(),
            "dst_widened_start_seconds": widened_start_seconds,
            "dst_widened_end_seconds": widened_end_seconds,
            "dst_widened_seconds": widened_start_seconds + widened_end_seconds,
        },
    )


def trips_wire_window(start: datetime, end: datetime) -> Tuple[str, str, Dict[str, Any]]:
    """Return the `/trips` request window as (start_str, end_str, diagnostics).

    Over-fetching is free here: `client_trips` upserts on
    `(client_id, provider_trip_id)`, so a duplicate row is a no-op update
    rather than a new row.
    """
    wire_start, wire_end, _effective_start, _effective_end, diagnostics = _wall_clock_wire_window(
        start, end,
    )
    return wire_start, wire_end, {
        "trips_request_timezone": TRIPS_REQUEST_TIMEZONE_NAME,
        **diagnostics,
    }


def _wall_clock_span(start_str: str, end_str: str) -> timedelta:
    """The numeral difference the provider sees between two wire strings."""
    return (
        datetime.strptime(end_str, PROVIDER_WIRE_DT_FORMAT)
        - datetime.strptime(start_str, PROVIDER_WIRE_DT_FORMAT)
    )


def vehicle_events_wire_window(start: datetime, end: datetime) -> Tuple[str, str, Dict[str, Any]]:
    """Return the `/vehicles/events` request window as (start_str, end_str, diagnostics).

    Same proven wall-clock/DST invariant as `trips_wire_window`, plus the
    endpoint's own maximum-window limit, which `/trips` does not share.

    Over-fetching is safe here too but for a different reason than `/trips`:
    the returned `event_ts` values are absolute UTC and every consumer matches
    events to trips on absolute timestamps, so an event fetched outside the
    intended interval simply matches no trip in that interval. Duplicate rows
    are dropped by the existing per-event dedupe keys.

    The limit is checked three times, on the three quantities that can differ:

      * the **intended** absolute span, against
        `VEHICLE_EVENTS_MAX_INTENDED_WINDOW`, so that a window which *could*
        widen past the provider limit is rejected before conversion rather than
        after a wasted request;
      * the **effective** absolute span after widening;
      * the **wall-clock** span actually written on the wire, which a
        spring-forward crossing inflates by one hour relative to the absolute
        span.

    A violation raises `ValueError` rather than a `TelematicsProviderSafetyError`:
    it is a caller programming/configuration error, not a provider condition,
    and it must not be absorbed by the adaptive chunk-reduction paths that
    retry provider failures.
    """
    if _to_utc(end) < _to_utc(start):
        raise ValueError("vehicle events end_timestamp must be >= start_timestamp")

    wire_start, wire_end, effective_start, effective_end, diagnostics = _wall_clock_wire_window(
        start, end,
    )

    effective_span = effective_end - effective_start
    intended_span = effective_span - timedelta(seconds=diagnostics["dst_widened_seconds"])
    if intended_span > VEHICLE_EVENTS_MAX_INTENDED_WINDOW:
        raise ValueError(
            "vehicle events intended window must be <= "
            f"{VEHICLE_EVENTS_MAX_INTENDED_WINDOW}; got {intended_span}. "
            "Split the window before serialization; the provider limit is "
            f"{VEHICLE_EVENTS_MAX_REQUEST_WINDOW} and DST widening adds up to 1 hour per side."
        )

    if effective_span >= VEHICLE_EVENTS_MAX_REQUEST_WINDOW:
        raise ValueError(
            "DST-widened vehicle events window must be < "
            f"{VEHICLE_EVENTS_MAX_REQUEST_WINDOW}; got {effective_span}"
        )

    wall_clock_span = _wall_clock_span(wire_start, wire_end)
    if wall_clock_span >= VEHICLE_EVENTS_MAX_REQUEST_WINDOW:
        raise ValueError(
            "vehicle events wire wall-clock span must be < "
            f"{VEHICLE_EVENTS_MAX_REQUEST_WINDOW}; got {wall_clock_span}"
        )

    return wire_start, wire_end, {
        "vehicle_events_request_timezone": VEHICLE_EVENTS_REQUEST_TIMEZONE_NAME,
        **diagnostics,
        "effective_window_seconds": int(effective_span.total_seconds()),
        "wire_wall_clock_span_seconds": int(wall_clock_span.total_seconds()),
    }


def _safe_response_text(response: Any, *, max_chars: int = 4000) -> Optional[str]:
    if response is None:
        return None
    try:
        text = response.text
    except Exception as exc:
        return f"<failed to read response text: {type(exc).__name__}>"
    if text is None:
        return None
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...<truncated>"


_SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "date",
    "server",
    "x-request-id",
    "x-correlation-id",
    "x-trace-id",
    "traceparent",
    "cf-ray",
}


def _safe_response_headers(response: Any) -> Dict[str, str]:
    if response is None:
        return {}
    headers = getattr(response, "headers", None)
    if not headers:
        return {}

    safe: Dict[str, str] = {}
    try:
        iterable = headers.items()
    except AttributeError:
        return {}
    for key, value in iterable:
        header_name = str(key).strip().lower()
        if header_name not in _SAFE_RESPONSE_HEADERS:
            continue
        safe[header_name] = str(value)[:500]
    return safe


def _provider_wall_clock_dt_str(dt: datetime) -> str:
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def sub_window_label(start: datetime, end: datetime) -> str:
    return f"{_provider_dt_str(start)}..{_provider_dt_str(end)}"


def _page_items_fingerprint(page_items: List[Dict[str, Any]]) -> str:
    raw = json.dumps(page_items, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# `/trips` compatibility pagination (`data_invariants_v1`) helpers.
#
# Identity is the production ingestion business key component only:
# `provider_trip_id = int(row["trip_id"])`, the `client_trips` conflict target.
# No timestamp, coordinate, address, registration, driver or other mutable trip
# attribute may ever act as a fallback identity (docs/12 §5.1).
# ---------------------------------------------------------------------------

COMPAT_IDENTITY_FIELD = "trip_id"
COMPAT_IDENTITY_SOURCE = "provider_trip_id"
COMPAT_IDENTITY_DEFECT_MISSING = "missing"
COMPAT_IDENTITY_DEFECT_MALFORMED = "malformed"
COMPAT_REPEAT_ORDERED = "ordered_fingerprint"
COMPAT_REPEAT_UNORDERED = "unordered_identity_set"
COMPAT_TOTAL_STATE_ABSENT = "absent"
COMPAT_TOTAL_STATE_PRESENT = "present"
COMPAT_TOTAL_RECONCILIATION_EXACT = "exact"
_COMPAT_UNIT_SEPARATOR = "\x1f"


def _compat_trip_identity(row: Any) -> Tuple[Optional[int], Optional[str]]:
    """Return `(provider_trip_id, defect)` for one `/trips` row.

    Deterministic and identical to what ingestion would key on. `defect` is
    `None` on success, otherwise `missing` or `malformed`.
    """
    if not isinstance(row, dict) or COMPAT_IDENTITY_FIELD not in row:
        return None, COMPAT_IDENTITY_DEFECT_MISSING
    raw = row.get(COMPAT_IDENTITY_FIELD)
    if raw is None:
        return None, COMPAT_IDENTITY_DEFECT_MISSING
    if isinstance(raw, bool):
        return None, COMPAT_IDENTITY_DEFECT_MALFORMED
    if isinstance(raw, float) and not raw.is_integer():
        return None, COMPAT_IDENTITY_DEFECT_MALFORMED
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, COMPAT_IDENTITY_DEFECT_MALFORMED


def _compat_identity_digest(provider_trip_id: int, *, salt: bytes) -> str:
    material = f"{COMPAT_IDENTITY_SOURCE}{_COMPAT_UNIT_SEPARATOR}{provider_trip_id}"
    return hmac.new(salt, material.encode("utf-8"), hashlib.sha256).hexdigest()


def _compat_page_fingerprints(digests: List[str]) -> Tuple[str, str]:
    """Ordered and unordered identity digests for one page.

    Computed over identities only, never over the raw payload, so a cosmetic
    field change cannot mask a genuine repeat (docs/12 §5.2 P2).
    """
    ordered = hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()
    unordered = hashlib.sha256("\n".join(sorted(set(digests))).encode("utf-8")).hexdigest()
    return ordered, unordered


def _compat_payload_bytes(payload: Any) -> int:
    """Deterministic normalized size of one parsed response body.

    The strict transport path (`_request_json`) is deliberately left untouched,
    so the byte budget is measured over the canonical re-serialization of the
    parsed payload rather than over the socket.
    """
    try:
        return len(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _compat_meta_type(payload: Any) -> str:
    if not isinstance(payload, dict) or "meta" not in payload:
        return "absent"
    meta = payload.get("meta")
    if meta is None:
        return "null"
    if isinstance(meta, dict):
        return "object"
    if isinstance(meta, list):
        return "list"
    return type(meta).__name__


def _compat_diagnostic_meta(payload: Any) -> Dict[str, Any]:
    """Broken metadata, captured for diagnostics only — never control flow."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    if not isinstance(meta, dict):
        return {
            "meta_current_page": None,
            "meta_per_page": None,
            "meta_last_page": None,
            "meta_from": None,
            "meta_to": None,
        }
    out: Dict[str, Any] = {}
    for field_name in ("current_page", "per_page", "last_page", "from", "to"):
        value = meta.get(field_name)
        out[f"meta_{field_name}"] = value if isinstance(value, int) and not isinstance(value, bool) else None
    return out


def _parse_provider_dt_optional(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
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
            try:
                dt = datetime.fromisoformat(raw.replace(" ", "T"))
            except ValueError:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _optional_float(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:
        return None
    return value


def _optional_int(raw: Any) -> Optional[int]:
    value = _optional_float(raw)
    if value is None:
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
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
    if isinstance(raw, (int, float)):
        return bool(raw)
    return None


def _normalize_vehicle_event(item: Dict[str, Any], *, default_registration: str = "") -> Dict[str, Any]:
    return {
        "event_id": item.get("event_id"),
        "registration": str(item.get("registration") or default_registration).strip(),
        "vehicle_id": item.get("vehicle_id"),
        "event_ts": _parse_provider_dt_optional(item.get("event_ts")),
        "speed": _optional_float(item.get("speed")),
        "road_speed": _optional_float(item.get("road_speed")),
        "road_speeding": _optional_bool(item.get("road_speeding")),
        "rpm": _optional_int(item.get("rpm")),
        "latitude": _optional_float(item.get("latitude")),
        "longitude": _optional_float(item.get("longitude")),
        "odometer": _optional_int(item.get("odometer")),
        "raw": item,
    }


def _optional_text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _normalize_vehicle_inventory_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "vehicle_id": item.get("vehicle_id"),
        "registration": str(item.get("registration") or "").strip(),
        "vehicle_name": _optional_text(item.get("vehicle_name")),
        "vehicle_description": _optional_text(item.get("client_vehicle_description")),
        "chassis_number": _optional_text(item.get("chassis_number")),
        "raw": item,
    }


def _normalize_driver_inventory_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "driver_id": _optional_text(item.get("driver_id")),
        "first_name": _optional_text(item.get("first_name")),
        "last_name": _optional_text(item.get("last_name")),
        "identification_tag_id": _optional_text(
            item.get("identification_tag_id")
            or item.get("driver_identification_tag_id")
            or item.get("last_identification_tag_id")
            or item.get("identification_tag")
        ),
        "license_driver_restrictions": _optional_text(item.get("license_driver_restrictions")),
        "raw": item,
    }


class TelematicsFleetProviderClient:
    """
    Telematics Fleet HTTP client with hard safety limits (Phase 2).

    On any safety breach: raises TelematicsProviderSafetyError and must not continue requests.
    """

    def __init__(
        self,
        *,
        base_url: str,
        basic_auth_username: str,
        basic_auth_password: str,
        timeout_s: Optional[int] = None,
        page_limit: int = TELEMATICS_PROVIDER_DEFAULT_PAGE_LIMIT,
        safety_limits: Optional[SafetyLimits] = None,
        budget: Optional[ProviderRunBudget] = None,
        log_fn: Optional[LogFn] = None,
        rate_limit_rps: Optional[float] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        trips_pagination_mode: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        limits = safety_limits or SafetyLimits()
        self.limits = limits
        self.timeout_s = timeout_s if timeout_s is not None else limits.timeout_s
        self.page_limit = page_limit
        self.budget = budget or ProviderRunBudget(limits=limits)
        self.log_fn = log_fn
        # Fail closed: `None` resolves to `strict_meta`, anything unknown raises
        # before a single request is issued. The mode is frozen for the whole
        # client lifetime and is never switched mid-sequence.
        self.trips_pagination_mode = normalize_trips_pagination_mode(trips_pagination_mode)
        # M4 request-evidence sink (`jobs.api.telematics.request_evidence`). Set by
        # the job for a compatibility `/trips` execution and left `None`
        # everywhere else, which keeps every other caller's behaviour
        # byte-identical. It only ever *observes*: no branch of the fetch
        # contract reads it, so it cannot change what is requested, retried,
        # accepted or aborted.
        self.request_evidence = None
        #: Status code of the most recent successful provider response, so the
        #: compatibility loop can record what actually came back rather than
        #: assuming 200 for everything `raise_for_status()` let through.
        self._last_response_status: Optional[int] = None
        self._compat_limits: Optional[CompatibilitySafetyLimits] = None
        # Per-execution HMAC salt: in memory only, never persisted, never logged
        # (docs/12 §10.3). Only the compatibility path uses it.
        self._compat_identity_salt = secrets.token_bytes(32)
        self.rate_limit_rps = rate_limit_rps if rate_limit_rps and rate_limit_rps > 0 else None
        self._sleep_fn = sleep_fn or time.sleep
        self._last_request_monotonic: Optional[float] = None
        self._session = requests.Session()
        self._session.auth = (basic_auth_username, basic_auth_password)
        self._metrics: Dict[str, Any] = {
            "total_requests": 0,
            "request_count_by_endpoint": {},
            "response_parse_count_by_endpoint": {},
            "request_elapsed_seconds_by_endpoint": {},
            "response_parse_elapsed_seconds_by_endpoint": {},
        }

    def _log(self, level: str, message: str, context: Dict[str, Any]) -> None:
        if self.log_fn:
            ctx = dict(context)
            ctx.update(self.budget.accounting_context())
            self.log_fn(level, message, ctx)

    def _record_metric_count(self, key: str, endpoint: str, count: int = 1) -> None:
        bucket = self._metrics.setdefault(key, {})
        bucket[endpoint] = int(bucket.get(endpoint, 0)) + count

    def _record_metric_elapsed(self, key: str, endpoint: str, elapsed_s: float) -> None:
        bucket = self._metrics.setdefault(key, {})
        bucket[endpoint] = float(bucket.get(endpoint, 0.0)) + max(0.0, elapsed_s)

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Return provider request timing counters for run-level instrumentation."""
        return {
            "total_requests": int(self._metrics.get("total_requests", 0)),
            "request_count_by_endpoint": dict(self._metrics.get("request_count_by_endpoint", {})),
            "response_parse_count_by_endpoint": dict(
                self._metrics.get("response_parse_count_by_endpoint", {})
            ),
            "request_elapsed_seconds_by_endpoint": dict(
                self._metrics.get("request_elapsed_seconds_by_endpoint", {})
            ),
            "response_parse_elapsed_seconds_by_endpoint": dict(
                self._metrics.get("response_parse_elapsed_seconds_by_endpoint", {})
            ),
        }

    def _throttle_provider_requests(self) -> None:
        if not self.rate_limit_rps:
            return
        min_interval_s = 1.0 / self.rate_limit_rps
        now = time.monotonic()
        if self._last_request_monotonic is not None:
            elapsed_s = now - self._last_request_monotonic
            sleep_s = min_interval_s - elapsed_s
            if sleep_s > 0:
                self._sleep_fn(sleep_s)
        self._last_request_monotonic = time.monotonic()

    def _retry_backoff_seconds(self, attempt: int) -> int:
        # attempt is 1-based and represents the failed attempt before retrying.
        if attempt <= 1:
            return 5
        return 15

    def _request_json(
        self,
        *,
        path: str,
        params: Dict[str, Any],
        sub_window_label: str,
        budget_path: Optional[str] = None,
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Single GET with bounded retries (timeouts / connection errors only).
        """
        url = f"{self.base_url}{path}"
        accounting_path = budget_path or path
        max_attempts = 1 + self.limits.max_retries_per_http_call

        for attempt in range(1, max_attempts + 1):
            self.budget.record_request_issued(path=accounting_path, sub_window_label=sub_window_label)
            self._log(
                "INFO",
                "telematics_provider_request",
                {
                    "endpoint": accounting_path,
                    "sub_window": sub_window_label,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "params": dict(params),
                },
            )
            request_started_at = time.monotonic()
            try:
                self._throttle_provider_requests()
                r = self._session.get(url, params=params, timeout=timeout_s or self.timeout_s)
                r.raise_for_status()
                # Observational only, and deliberately tolerant: M4 evidence
                # must never be able to break a provider call. A response object
                # without a usable status leaves this `None`, which drops the
                # page record and makes the sub-window INCOMPLETE — fail-closed
                # on the coverage side, with the fetch itself untouched.
                status = getattr(r, "status_code", None)
                self._last_response_status = (
                    int(status) if isinstance(status, int) else None
                )
                request_elapsed_s = time.monotonic() - request_started_at
                self._metrics["total_requests"] = int(self._metrics.get("total_requests", 0)) + 1
                self._record_metric_count("request_count_by_endpoint", accounting_path)
                self._record_metric_elapsed(
                    "request_elapsed_seconds_by_endpoint",
                    accounting_path,
                    request_elapsed_s,
                )
                parse_started_at = time.monotonic()
                try:
                    payload = r.json()
                finally:
                    self._record_metric_count("response_parse_count_by_endpoint", accounting_path)
                    self._record_metric_elapsed(
                        "response_parse_elapsed_seconds_by_endpoint",
                        accounting_path,
                        time.monotonic() - parse_started_at,
                    )
                if not isinstance(payload, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider response JSON is not an object: {path}",
                        context={
                            "endpoint": accounting_path,
                            "sub_window": sub_window_label,
                            "params": dict(params),
                            "type": type(payload).__name__,
                        },
                    )
                return payload
            except TelematicsProviderSafetyError:
                raise
            except (RequestsTimeout, RequestsConnectionError) as e:
                request_elapsed_s = time.monotonic() - request_started_at
                self._metrics["total_requests"] = int(self._metrics.get("total_requests", 0)) + 1
                self._record_metric_count("request_count_by_endpoint", accounting_path)
                self._record_metric_elapsed(
                    "request_elapsed_seconds_by_endpoint",
                    accounting_path,
                    request_elapsed_s,
                )
                self._log(
                    "WARNING",
                    "telematics_provider_request_retryable",
                    {
                        "endpoint": accounting_path,
                        "sub_window": sub_window_label,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "params": dict(params),
                        "error": type(e).__name__,
                        "detail": str(e),
                        "backoff_seconds": None if attempt >= max_attempts else self._retry_backoff_seconds(attempt),
                    },
                )
                if attempt >= max_attempts:
                    raise TelematicsProviderSafetyError(
                        "HTTP_RETRY_EXHAUSTED",
                        f"Exceeded TELEMATICS_PROVIDER_MAX_RETRIES ({self.limits.max_retries_per_http_call}) for {path}",
                        context={
                            "endpoint": accounting_path,
                            "sub_window": sub_window_label,
                            "attempts": attempt,
                            "params": dict(params),
                            "last_error": type(e).__name__,
                            "detail": str(e),
                        },
                    ) from e
                self._sleep_fn(self._retry_backoff_seconds(attempt))
            except RequestsHTTPError as e:
                request_elapsed_s = time.monotonic() - request_started_at
                self._metrics["total_requests"] = int(self._metrics.get("total_requests", 0)) + 1
                self._record_metric_count("request_count_by_endpoint", accounting_path)
                self._record_metric_elapsed(
                    "request_elapsed_seconds_by_endpoint",
                    accounting_path,
                    request_elapsed_s,
                )
                response = e.response
                raise TelematicsProviderSafetyError(
                    "HTTP_ERROR",
                    f"Provider HTTP error: {path} status={getattr(response, 'status_code', None)}",
                    context={
                        "endpoint": accounting_path,
                        "sub_window": sub_window_label,
                        "status_code": getattr(response, "status_code", None),
                        "params": dict(params),
                        "response_body_text": _safe_response_text(response),
                        "response_headers": _safe_response_headers(response),
                    },
                ) from e
            except json.JSONDecodeError as e:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider response is not valid JSON: {path}",
                    context={
                        "endpoint": accounting_path,
                        "sub_window": sub_window_label,
                        "params": dict(params),
                        "detail": str(e),
                    },
                ) from e

    def _parse_pagination_meta(self, meta: Any, *, path: str, sub_window_label: str) -> Tuple[Optional[int], Optional[int]]:
        if meta is None:
            return None, None
        if not isinstance(meta, dict):
            raise TelematicsProviderSafetyError(
                "MALFORMED_PAGINATION",
                f"Provider meta is not an object: {path}",
                context={"endpoint": path, "sub_window": sub_window_label},
            )
        current_page = meta.get("current_page")
        last_page = meta.get("last_page")
        if current_page is not None and not isinstance(current_page, int):
            try:
                current_page = int(current_page)
            except (TypeError, ValueError):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    f"Invalid current_page in meta: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "current_page": meta.get("current_page")},
                )
        if last_page is not None and not isinstance(last_page, int):
            try:
                last_page = int(last_page)
            except (TypeError, ValueError):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    f"Invalid last_page in meta: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "last_page": meta.get("last_page")},
                )
        if current_page is not None and current_page < 1:
            raise TelematicsProviderSafetyError(
                "MALFORMED_PAGINATION",
                f"current_page must be >= 1: {path}",
                context={"endpoint": path, "sub_window": sub_window_label, "current_page": current_page},
            )
        if last_page is not None and last_page < 0:
            raise TelematicsProviderSafetyError(
                "MALFORMED_PAGINATION",
                f"last_page must be >= 0: {path}",
                context={"endpoint": path, "sub_window": sub_window_label, "last_page": last_page},
            )
        if current_page is not None and last_page is not None and last_page > 0 and current_page > last_page:
            raise TelematicsProviderSafetyError(
                "INCONSISTENT_PAGINATION",
                f"current_page > last_page: {path}",
                context={
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "current_page": current_page,
                    "last_page": last_page,
                },
            )
        return current_page, last_page

    def _fetch_paginated(
        self,
        *,
        path: str,
        base_params: Dict[str, Any],
        sub_window_label: str,
        page_param: str = "page",
        limit_param: str = "limit",
        budget_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        accounting_path = budget_path or path
        prev_fingerprint: Optional[str] = None
        prev_requested_page: Optional[int] = None
        empty_streak = 0

        while True:
            params = dict(base_params)
            params[page_param] = page
            params[limit_param] = self.page_limit

            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
                budget_path=accounting_path,
            )
            # Count only successful HTTP responses (after bounded retries).
            self.budget.record_page_completed(path=accounting_path, sub_window_label=sub_window_label, page_index=page)

            page_items = payload.get("data")
            if page_items is None:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )
            if not isinstance(page_items, list):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )

            fp = _page_items_fingerprint(page_items)
            if prev_requested_page is not None and prev_requested_page == page and fp == prev_fingerprint:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_LOOP",
                    "Repeated identical page payload fingerprint for same requested page (suspected pagination loop)",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "page": page,
                        "fingerprint": fp[:16],
                    },
                )

            meta_raw = payload.get("meta")
            current_page, last_page = self._parse_pagination_meta(meta_raw, path=path, sub_window_label=sub_window_label)

            if (current_page is None) != (last_page is None):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    "Partial pagination meta: current_page and last_page must both be present or both absent",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            # Strict: response current_page must match requested page when meta is present (1-based API).
            if current_page is not None and current_page != page:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_MISMATCH",
                    "Response meta current_page does not match requested page (suspected loop or API mismatch)",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            # Same non-empty payload twice in a row => no forward progress.
            if prev_fingerprint is not None and fp == prev_fingerprint and len(page_items) > 0:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_LOOP",
                    "Repeated identical non-empty page fingerprint (suspected pagination loop)",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )

            if len(page_items) == 0:
                empty_streak += 1
                if (
                    empty_streak >= 2
                    and last_page is not None
                    and current_page is not None
                    and current_page < last_page
                ):
                    raise TelematicsProviderSafetyError(
                        "PAGINATION_NON_PROGRESS",
                        "Repeated empty pages while meta indicates more pages remain",
                        context={
                            "endpoint": path,
                            "sub_window": sub_window_label,
                            "page": page,
                            "current_page": current_page,
                            "last_page": last_page,
                            "empty_streak": empty_streak,
                        },
                    )
            else:
                empty_streak = 0

            items.extend(page_items)
            prev_fingerprint = fp
            prev_requested_page = page

            self._log(
                "INFO",
                "telematics_provider_page",
                {
                    "endpoint": accounting_path,
                    "sub_window": sub_window_label,
                    "requested_page": page,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                    "page_items": len(page_items),
                },
            )

            if last_page is None or current_page is None:
                if page > 1:
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_PAGINATION",
                        f"Missing pagination meta after first page: {path}",
                        context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                    )
                return items

            if last_page == 0:
                return items

            if current_page > last_page:
                raise TelematicsProviderSafetyError(
                    "INCONSISTENT_PAGINATION",
                    "current_page exceeds last_page after response",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "current_page": current_page,
                        "last_page": last_page,
                    },
                )

            if current_page >= last_page:
                return items

            page += 1
            if page > last_page + 1:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_NON_PROGRESS",
                    "Requested page exceeds last_page+1 (abort)",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "last_page": last_page,
                    },
                )

    def _compatibility_limits(self) -> CompatibilitySafetyLimits:
        """Validate the compatibility budgets exactly once, before any request."""
        if self._compat_limits is None:
            self._compat_limits = CompatibilitySafetyLimits.from_env(
                page_limit=self.page_limit,
                max_pages_per_subwindow=self.limits.max_pages_per_subwindow,
            )
        return self._compat_limits

    def _fetch_paginated_data_invariants_v1(
        self,
        *,
        path: str,
        base_params: Dict[str, Any],
        sub_window_label: str,
        page_param: str = "page",
        limit_param: str = "limit",
        budget_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Compatibility pagination for one `/trips` sub-window.

        Implements docs/12 §4-§8 and the accepted docs/16 D5 Option B policy.
        Control flow is derived exclusively from returned data: `meta.current_page`,
        `meta.per_page`, `meta.last_page`, `meta.from` and `meta.to` are captured
        as diagnostics and never read by any transition. `meta.total` is advisory
        only — it never selects a page, never continues a loop and never
        terminates one; its absence is a permitted state.

        The whole sub-window is validated before any row is returned to the
        caller: a failure on a later page discards every earlier page.

            INIT -> REQUEST -> VALIDATE_RESPONSE -> EXTRACT_IDENTITIES
                 -> PAGE_LOCAL_CHECKS -> CROSS_PAGE_CHECKS -> ACCUMULATE
                 -> TERMINATE? -> CONTINUE | RECONCILE -> SUCCESS
        """
        # --- INIT ---
        accounting_path = budget_path or path
        compat = self._compatibility_limits()
        limit = self.page_limit
        salt = self._compat_identity_salt

        rows: List[Dict[str, Any]] = []
        seen_ids: Set[int] = set()
        seen_ordered_fingerprints: Set[str] = set()
        seen_identity_sets: Set[str] = set()
        page = 1
        pages_fetched = 0
        rows_total = 0
        bytes_total = 0
        total_state: Optional[str] = None
        advisory_total: Optional[int] = None
        meta_type: Optional[str] = None
        started_at = time.monotonic()

        def _abort(code: str, message: str, context: Dict[str, Any]) -> "TelematicsProviderSafetyError":
            return TelematicsProviderSafetyError(
                code,
                message,
                context={
                    "endpoint": accounting_path,
                    "sub_window": sub_window_label,
                    "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
                    "requested_page": page,
                    "requested_limit": limit,
                    "pages_fetched": pages_fetched,
                    "accumulated_unique_rows": len(seen_ids),
                    **context,
                },
            )

        while True:
            # --- REQUEST: every budget is checked before the request is issued. ---
            if page != pages_fetched + 1:
                raise _abort(
                    "PAGINATION_NON_PROGRESS",
                    "Compatibility page sequence did not progress by exactly one",
                    {"expected_page": pages_fetched + 1},
                )
            if pages_fetched >= self.limits.max_pages_per_subwindow:
                raise _abort(
                    "MAX_PAGES_PER_SUBWINDOW",
                    f"Exceeded TELEMATICS_PROVIDER_MAX_PAGES_PER_SUBWINDOW ({self.limits.max_pages_per_subwindow})",
                    {"limit": self.limits.max_pages_per_subwindow},
                )
            elapsed_s = time.monotonic() - started_at
            if elapsed_s > compat.max_elapsed_s:
                raise _abort(
                    PAGINATION_COMPAT_ELAPSED_BUDGET_EXCEEDED,
                    f"Compatibility sub-window exceeded {compat.max_elapsed_s}s",
                    {"elapsed_seconds": round(elapsed_s, 3), "limit": compat.max_elapsed_s},
                )
            self.budget.before_request(path=accounting_path, sub_window_label=sub_window_label)

            params = dict(base_params)
            params[page_param] = page
            params[limit_param] = limit
            # Wall-clock instants, not the monotonic clock the budgets use: the
            # publication-lag bounds (`docs/20` §4.4) are differences between an
            # absolute response instant and a trip's start, so they need real
            # UTC. Taken around the whole call, retries included, because
            # "when did this request start" is honestly the first attempt.
            evidence_started_at = datetime.now(timezone.utc)
            self._last_response_status = None
            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
                budget_path=accounting_path,
            )
            evidence_received_at = datetime.now(timezone.utc)
            # Count only successful HTTP responses, exactly as the strict path does.
            self.budget.record_page_completed(
                path=accounting_path, sub_window_label=sub_window_label, page_index=page,
            )
            pages_fetched += 1

            page_bytes = _compat_payload_bytes(payload)
            if page_bytes > compat.max_response_bytes:
                raise _abort(
                    PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED,
                    "Compatibility response exceeded the per-response byte budget",
                    {
                        "response_bytes": page_bytes,
                        "limit": compat.max_response_bytes,
                        "scope": "response",
                    },
                )
            bytes_total += page_bytes
            if bytes_total > compat.max_response_bytes_per_subwindow:
                raise _abort(
                    PAGINATION_COMPAT_RESPONSE_BYTES_EXCEEDED,
                    "Compatibility sub-window exceeded the accumulated byte budget",
                    {
                        "response_bytes": page_bytes,
                        "accumulated_response_bytes": bytes_total,
                        "limit": compat.max_response_bytes_per_subwindow,
                        "scope": "sub_window",
                    },
                )

            # --- VALIDATE_RESPONSE ---
            data = payload.get("data")
            if data is None:
                raise _abort(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    {},
                )
            if not isinstance(data, list):
                raise _abort(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    {"data_type": type(data).__name__},
                )
            page_meta_type = _compat_meta_type(payload)
            if meta_type is None:
                meta_type = page_meta_type
            elif page_meta_type != meta_type:
                raise _abort(
                    PAGINATION_COMPAT_SHAPE_UNSTABLE,
                    "Provider meta changed JSON type between pages of one sub-window",
                    {"meta_type": page_meta_type, "first_meta_type": meta_type},
                )

            # --- EXTRACT_IDENTITIES ---
            page_ids: List[int] = []
            page_digests: List[str] = []
            for index, row in enumerate(data):
                if not isinstance(row, dict):
                    raise _abort(
                        "MALFORMED_RESPONSE",
                        f"Provider data item is not an object: {path}",
                        {"row_index": index, "row_type": type(row).__name__},
                    )
                provider_trip_id, defect = _compat_trip_identity(row)
                if defect is not None:
                    raise _abort(
                        PAGINATION_COMPAT_IDENTITY_MISSING,
                        f"Provider trip row has no usable stable identity ({defect})",
                        {"row_index": index, "identity_defect": defect},
                    )
                page_ids.append(provider_trip_id)
                page_digests.append(_compat_identity_digest(provider_trip_id, salt=salt))

            # --- PAGE_LOCAL_CHECKS ---
            if len(data) > limit:
                raise _abort(
                    PAGINATION_COMPAT_ROWS_EXCEED_LIMIT,
                    "Provider returned more rows than the requested limit",
                    {"returned_count": len(data)},
                )
            unique_page_ids = set(page_ids)
            if len(unique_page_ids) != len(page_ids):
                raise _abort(
                    PAGINATION_COMPAT_DUPLICATE_IN_PAGE,
                    "Duplicate trip identity inside one page",
                    {
                        "returned_count": len(page_ids),
                        "unique_identity_count": len(unique_page_ids),
                    },
                )
            fingerprint_ordered, fingerprint_unordered = _compat_page_fingerprints(page_digests)

            # --- CROSS_PAGE_CHECKS (full sub-window history, not just the previous page) ---
            if fingerprint_ordered in seen_ordered_fingerprints:
                raise _abort(
                    PAGINATION_COMPAT_PAGE_REPEATED,
                    "Repeated ordered identity fingerprint within the sub-window",
                    {
                        "repeat_kind": COMPAT_REPEAT_ORDERED,
                        "page_identity_fingerprint": fingerprint_ordered[:16],
                    },
                )
            if fingerprint_unordered in seen_identity_sets:
                raise _abort(
                    PAGINATION_COMPAT_PAGE_REPEATED,
                    "Repeated unordered identity set within the sub-window",
                    {
                        "repeat_kind": COMPAT_REPEAT_UNORDERED,
                        "page_identity_set_fingerprint": fingerprint_unordered[:16],
                    },
                )
            overlap = seen_ids & unique_page_ids
            if overlap:
                raise _abort(
                    PAGINATION_COMPAT_PAGE_OVERLAP,
                    "Trip identity already seen on an earlier page of the sub-window",
                    {
                        "overlap_count": len(overlap),
                        "returned_count": len(page_ids),
                        "page_identity_fingerprint": fingerprint_ordered[:16],
                    },
                )

            page_total_state, page_total = self._compat_read_advisory_total(
                payload, abort=_abort,
            )
            if total_state is None:
                total_state = page_total_state
                advisory_total = page_total
            elif page_total_state != total_state or page_total != advisory_total:
                raise _abort(
                    PAGINATION_COMPAT_TOTAL_UNSTABLE,
                    "Advisory meta.total is not identical on every page of the sub-window",
                    {
                        "total_present": page_total_state == COMPAT_TOTAL_STATE_PRESENT,
                        "first_total_present": total_state == COMPAT_TOTAL_STATE_PRESENT,
                        "advisory_total": page_total,
                        "first_advisory_total": advisory_total,
                    },
                )

            # --- ACCUMULATE (provider order preserved; rows are never sorted) ---
            rows.extend(data)
            rows_total += len(data)
            seen_ids |= unique_page_ids
            seen_ordered_fingerprints.add(fingerprint_ordered)
            seen_identity_sets.add(fingerprint_unordered)

            # --- M4 evidence: one record per fully validated page request, and
            # the first-seen binding for every identity it introduced. Recorded
            # here rather than at the request, so a page that failed a
            # validation above is never described as a page that returned. The
            # sink is observational: nothing below reads what it stored.
            if self.request_evidence is not None:
                observed_status = self._last_response_status
                if observed_status is not None:
                    request_id = self.request_evidence.record_page(
                        page=page,
                        request_started_at_utc=evidence_started_at,
                        response_received_at_utc=evidence_received_at,
                        http_status=observed_status,
                        row_count=len(data),
                    )
                    # `page_ids` in provider order, not the set: first-seen
                    # provenance is about which request first returned a trip,
                    # and set iteration order would make that arbitrary.
                    self.request_evidence.record_first_seen(
                        request_id=request_id, identities=page_ids,
                    )

            if rows_total > compat.max_rows_per_subwindow:
                raise _abort(
                    PAGINATION_COMPAT_ROW_BUDGET_EXCEEDED,
                    f"Compatibility sub-window exceeded {compat.max_rows_per_subwindow} rows",
                    {"accumulated_count": rows_total, "limit": compat.max_rows_per_subwindow},
                )
            if advisory_total is not None and len(seen_ids) > advisory_total:
                raise _abort(
                    PAGINATION_COMPAT_TOTAL_EXCEEDED,
                    "Accumulated unique rows exceed the advisory meta.total",
                    {"advisory_total": advisory_total, "accumulated_count": rows_total},
                )

            short_page = len(data) < limit
            self._log(
                "INFO",
                "telematics_trips_compat_page",
                {
                    "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
                    "endpoint": accounting_path,
                    "sub_window": sub_window_label,
                    "requested_page": page,
                    "requested_limit": limit,
                    "returned_count": len(data),
                    "accumulated_count": rows_total,
                    "accumulated_unique_rows": len(seen_ids),
                    "unique_identity_count": len(unique_page_ids),
                    "overlap_count": 0,
                    "page_identity_fingerprint": fingerprint_ordered[:16],
                    "page_identity_set_fingerprint": fingerprint_unordered[:16],
                    "short_page": short_page,
                    "total_present": total_state == COMPAT_TOTAL_STATE_PRESENT,
                    "advisory_total": advisory_total,
                    "meta_type": page_meta_type,
                    **_compat_diagnostic_meta(payload),
                    "response_bytes": page_bytes,
                    "accumulated_response_bytes": bytes_total,
                    "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    "pages_remaining_subwindow": max(
                        0, self.limits.max_pages_per_subwindow - pages_fetched
                    ),
                    **compat.context(),
                },
            )

            # --- TERMINATE? Short page (including an empty page) is the only
            # authoritative signal. A full page always costs another request. ---
            if short_page:
                break
            page += 1

        # --- RECONCILE ---
        total_reconciliation = COMPAT_TOTAL_STATE_ABSENT
        if advisory_total is not None:
            accumulated_unique = len(seen_ids)
            if accumulated_unique > advisory_total:
                raise _abort(
                    PAGINATION_COMPAT_TOTAL_EXCEEDED,
                    "Accumulated unique rows exceed the advisory meta.total at termination",
                    {"advisory_total": advisory_total, "accumulated_count": rows_total},
                )
            if accumulated_unique < advisory_total:
                raise _abort(
                    PAGINATION_COMPAT_TOTAL_RECONCILIATION_FAILED,
                    "Short page reached while the advisory meta.total is higher than accumulated rows",
                    {"advisory_total": advisory_total, "accumulated_count": rows_total},
                )
            total_reconciliation = COMPAT_TOTAL_RECONCILIATION_EXACT

        # --- M4: the sub-window reached its one authoritative terminal state.
        # Every path that did not get here raised, so this marker is the
        # difference between "completed" and "was attempted" — and it is only
        # reachable after reconciliation, which is what makes an absent advisory
        # total a permitted COMPLETE and a failed reconciliation unreachable.
        if self.request_evidence is not None:
            self.request_evidence.complete_subwindow(
                termination_reason="short_page",
                total_reconciliation=total_reconciliation,
            )

        self._log(
            "INFO",
            "telematics_trips_compat_subwindow_summary",
            {
                "trips_pagination_mode": TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
                "endpoint": accounting_path,
                "sub_window": sub_window_label,
                "requested_limit": limit,
                "pages_fetched": pages_fetched,
                "accumulated_count": rows_total,
                "accumulated_unique_rows": len(seen_ids),
                "total_present": total_state == COMPAT_TOTAL_STATE_PRESENT,
                "advisory_total": advisory_total,
                "total_reconciliation": total_reconciliation,
                "termination_reason": "short_page",
                "accumulated_response_bytes": bytes_total,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                **compat.context(),
            },
        )
        # --- SUCCESS: released to the caller only now, fully validated. ---
        return rows

    def _compat_read_advisory_total(
        self,
        payload: Dict[str, Any],
        *,
        abort: Callable[[str, str, Dict[str, Any]], TelematicsProviderSafetyError],
    ) -> Tuple[str, Optional[int]]:
        """Read `meta.total` under the accepted D5 Option B rules (docs/16 §5).

        Absence is permitted and is not a safety incident. When present it must
        be a genuine, non-negative JSON integer: `bool`, `float`, numeric string,
        `null`, object and array are all invalid, and nothing is coerced.
        """
        meta = payload.get("meta")
        if not isinstance(meta, dict) or "total" not in meta:
            return COMPAT_TOTAL_STATE_ABSENT, None
        raw = meta.get("total")
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise abort(
                PAGINATION_COMPAT_TOTAL_INVALID,
                "Advisory meta.total is present but is not a JSON integer",
                {"total_type": "bool" if isinstance(raw, bool) else type(raw).__name__},
            )
        if raw < 0:
            raise abort(
                PAGINATION_COMPAT_TOTAL_INVALID,
                "Advisory meta.total is negative",
                {"total_type": "int", "advisory_total": raw},
            )
        return COMPAT_TOTAL_STATE_PRESENT, raw

    def fetch_trips(
        self,
        *,
        window_start_ts: datetime,
        window_end_ts: datetime,
        incl_private: bool = True,
    ) -> List[Dict[str, Any]]:
        """Fetch `/trips` for one execution window.

        The frozen `trips_pagination_mode` selects the pagination implementation
        once, per client instance. There is no auto-detection of the provider
        defect and no dynamic fallback in either direction. Every other endpoint
        keeps its existing strict implementation (docs/12 N7).
        """
        compatibility = self.trips_pagination_mode == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1
        all_items: List[Dict[str, Any]] = []
        for sub_start, sub_end in iter_31d_windows(
            window_start_ts, window_end_ts, max_days=TRIPS_MAX_SUB_WINDOW_DAYS,
        ):
            sw = sub_window_label(sub_start, sub_end)
            # `/trips` addresses trips by Europe/Warsaw wall-clock while
            # returning UTC rows; `sw` stays the UTC label so sub-window
            # identity, budget accounting and logs remain comparable.
            wire_start, wire_end, wire_context = trips_wire_window(sub_start, sub_end)
            self._log(
                "INFO",
                "telematics_trips_request_window",
                {
                    "endpoint": "/trips",
                    "sub_window": sw,
                    "wire_start_timestamp": wire_start,
                    "wire_end_timestamp": wire_end,
                    **wire_context,
                },
            )
            base_params = {
                "start_timestamp": wire_start,
                "end_timestamp": wire_end,
                "incl_private": str(bool(incl_private)).lower(),
            }
            # M4: open the evidence scope for this provider sub-window before
            # the first request, so a sub-window that dies mid-pagination is
            # still recorded as attempted-and-not-terminated rather than
            # vanishing. `sub_start`/`sub_end` are the absolute UTC instants;
            # the wire values are the Warsaw wall-clock numerals actually sent
            # (§1.4), and both are kept because only one of them is comparable
            # to a coverage window.
            if self.request_evidence is not None:
                self.request_evidence.begin_subwindow(
                    label=sw,
                    requested_from=sub_start,
                    requested_to=sub_end,
                    wire_start=wire_start,
                    wire_end=wire_end,
                )
            fetch = (
                self._fetch_paginated_data_invariants_v1
                if compatibility
                else self._fetch_paginated
            )
            all_items.extend(
                fetch(
                    path="/trips",
                    base_params=base_params,
                    sub_window_label=sw,
                )
            )
        return all_items

    def fetch_vehicles_fleet(
        self,
        *,
        limit: Optional[int] = None,
        max_pages: Optional[int] = None,
        max_records: Optional[int] = None,
        sub_window_label: str = "vehicle_inventory",
    ) -> List[Dict[str, Any]]:
        """Fetch the fleet-wide vehicle inventory via GET /vehicles.

        This is a batch-safe lookup used for per-run metadata enrichment.
        It intentionally does not call per-registration vehicle detail
        endpoints.
        """
        effective_limit = limit if limit is not None else self.page_limit
        effective_max_pages = max_pages if max_pages is not None else self.limits.max_pages_per_subwindow
        if effective_limit <= 0:
            raise ValueError("limit must be > 0")
        if effective_max_pages <= 0:
            raise ValueError("max_pages must be > 0")
        effective_max_records = (
            max_records
            if max_records is not None
            else effective_limit * effective_max_pages
        )
        if effective_max_records <= 0:
            raise ValueError("max_records must be > 0")

        path = "/vehicles"
        all_items: List[Dict[str, Any]] = []
        page = 1
        pages_fetched = 0
        meta_total: Optional[int] = None
        meta_last_page: Optional[int] = None
        stopped_by_empty = False
        stopped_by_max_pages = False
        stopped_by_max_records = False

        while True:
            if meta_last_page is not None and page > meta_last_page:
                self._log(
                    "WARNING",
                    "telematics_vehicle_inventory_page_beyond_last_page",
                    {
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_last_page": meta_last_page,
                    },
                )
                break

            if pages_fetched >= effective_max_pages:
                stopped_by_max_pages = True
                break

            params = {
                "page": page,
                "limit": effective_limit,
            }
            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
            )
            pages_fetched += 1
            self.budget.record_page_completed(path=path, sub_window_label=sub_window_label, page_index=page)

            page_items = payload.get("data")
            if page_items is None:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )
            if not isinstance(page_items, list):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )

            meta_raw = payload.get("meta")
            current_page, last_page = self._parse_pagination_meta(
                meta_raw, path=path, sub_window_label=sub_window_label,
            )
            if (current_page is None) != (last_page is None):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    "Partial pagination meta: current_page and last_page must both be present or both absent",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )
            if isinstance(meta_raw, dict):
                raw_total = meta_raw.get("total")
                if raw_total is not None:
                    try:
                        meta_total = int(raw_total)
                    except (TypeError, ValueError):
                        meta_total = None
            if last_page is not None:
                meta_last_page = last_page

            if current_page is not None and current_page != page:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_MISMATCH",
                    "Response meta current_page does not match requested page",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            self._log(
                "INFO",
                "telematics_vehicle_inventory_page",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "requested_page": page,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                    "meta_total": meta_total,
                    "limit": effective_limit,
                    "page_items": len(page_items),
                    "max_records": effective_max_records,
                },
            )

            if len(page_items) == 0:
                stopped_by_empty = True
                break

            remaining = effective_max_records - len(all_items)
            if remaining <= 0:
                stopped_by_max_records = True
                break

            if len(page_items) > remaining:
                stopped_by_max_records = True
                page_items = page_items[:remaining]

            for item in page_items:
                if not isinstance(item, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider data item is not an object: {path}",
                        context={"endpoint": path, "sub_window": sub_window_label, "type": type(item).__name__},
                    )
                all_items.append(_normalize_vehicle_inventory_item(item))

            if stopped_by_max_records:
                break

            if current_page is not None and last_page is not None and current_page >= last_page:
                break

            page += 1

        if stopped_by_max_pages:
            self._log(
                "WARNING",
                "telematics_vehicle_inventory_max_pages_reached",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "limit": effective_limit,
                    "max_pages": effective_max_pages,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "rows_returned": len(all_items),
                    "stopped_by_max_pages": True,
                },
            )

        if stopped_by_max_records:
            self._log(
                "WARNING",
                "telematics_vehicle_inventory_max_records_reached",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "limit": effective_limit,
                    "max_records": effective_max_records,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "rows_returned": len(all_items),
                    "stopped_by_max_records": True,
                },
            )

        self._log(
            "INFO",
            "telematics_vehicle_inventory_pagination_summary",
            {
                "endpoint": path,
                "sub_window": sub_window_label,
                "limit": effective_limit,
                "pages_fetched": pages_fetched,
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "stopped_by_max_records": stopped_by_max_records,
                "rows_returned": len(all_items),
            },
        )

        return all_items

    def fetch_drivers_fleet(
        self,
        *,
        limit: Optional[int] = None,
        max_pages: Optional[int] = None,
        max_records: Optional[int] = None,
        sub_window_label: str = "driver_inventory",
    ) -> List[Dict[str, Any]]:
        """Fetch the fleet-wide driver inventory via GET /drivers.

        This is a batch-safe lookup used for per-run driver metadata
        enrichment. It intentionally does not call per-driver detail endpoints.
        """
        effective_limit = limit if limit is not None else self.page_limit
        effective_max_pages = max_pages if max_pages is not None else self.limits.max_pages_per_subwindow
        if effective_limit <= 0:
            raise ValueError("limit must be > 0")
        if effective_max_pages <= 0:
            raise ValueError("max_pages must be > 0")
        effective_max_records = (
            max_records
            if max_records is not None
            else effective_limit * effective_max_pages
        )
        if effective_max_records <= 0:
            raise ValueError("max_records must be > 0")

        path = "/drivers"
        all_items: List[Dict[str, Any]] = []
        page = 1
        pages_fetched = 0
        meta_total: Optional[int] = None
        meta_last_page: Optional[int] = None
        stopped_by_empty = False
        stopped_by_max_pages = False
        stopped_by_max_records = False

        while True:
            if meta_last_page is not None and page > meta_last_page:
                self._log(
                    "WARNING",
                    "telematics_driver_inventory_page_beyond_last_page",
                    {
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_last_page": meta_last_page,
                    },
                )
                break

            if pages_fetched >= effective_max_pages:
                stopped_by_max_pages = True
                break

            params = {
                "page": page,
                "limit": effective_limit,
            }
            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
            )
            pages_fetched += 1
            self.budget.record_page_completed(path=path, sub_window_label=sub_window_label, page_index=page)

            page_items = payload.get("data")
            if page_items is None:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )
            if not isinstance(page_items, list):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )

            meta_raw = payload.get("meta")
            current_page, last_page = self._parse_pagination_meta(
                meta_raw, path=path, sub_window_label=sub_window_label,
            )
            if (current_page is None) != (last_page is None):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    "Partial pagination meta: current_page and last_page must both be present or both absent",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )
            if isinstance(meta_raw, dict):
                raw_total = meta_raw.get("total")
                if raw_total is not None:
                    try:
                        meta_total = int(raw_total)
                    except (TypeError, ValueError):
                        meta_total = None
            if last_page is not None:
                meta_last_page = last_page

            if current_page is not None and current_page != page:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_MISMATCH",
                    "Response meta current_page does not match requested page",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            self._log(
                "INFO",
                "telematics_driver_inventory_page",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "requested_page": page,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                    "meta_total": meta_total,
                    "limit": effective_limit,
                    "page_items": len(page_items),
                    "max_records": effective_max_records,
                },
            )

            if len(page_items) == 0:
                stopped_by_empty = True
                break

            remaining = effective_max_records - len(all_items)
            if remaining <= 0:
                stopped_by_max_records = True
                break

            if len(page_items) > remaining:
                stopped_by_max_records = True
                page_items = page_items[:remaining]

            for item in page_items:
                if not isinstance(item, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider data item is not an object: {path}",
                        context={"endpoint": path, "sub_window": sub_window_label, "type": type(item).__name__},
                    )
                all_items.append(_normalize_driver_inventory_item(item))

            if stopped_by_max_records:
                break

            if current_page is not None and last_page is not None and current_page >= last_page:
                break

            page += 1

        if stopped_by_max_pages:
            self._log(
                "WARNING",
                "telematics_driver_inventory_max_pages_reached",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "limit": effective_limit,
                    "max_pages": effective_max_pages,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "rows_returned": len(all_items),
                    "stopped_by_max_pages": True,
                },
            )

        if stopped_by_max_records:
            self._log(
                "WARNING",
                "telematics_driver_inventory_max_records_reached",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "limit": effective_limit,
                    "max_records": effective_max_records,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "rows_returned": len(all_items),
                    "stopped_by_max_records": True,
                },
            )

        self._log(
            "INFO",
            "telematics_driver_inventory_pagination_summary",
            {
                "endpoint": path,
                "sub_window": sub_window_label,
                "limit": effective_limit,
                "pages_fetched": pages_fetched,
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "stopped_by_max_records": stopped_by_max_records,
                "rows_returned": len(all_items),
            },
        )

        return all_items

    def fetch_fuel_consumed(
        self,
        *,
        registration: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        sub_window_label: str,
    ) -> Optional[float]:
        """Fetch fuel consumed for a single vehicle/time range. Returns liters or None."""
        path = f"/fuel/consumed/{registration}"
        params = {
            "start_timestamp": _provider_dt_str(start_timestamp),
            "end_timestamp": _provider_dt_str(end_timestamp),
        }
        try:
            payload = self._request_json(
                path=path, params=params, sub_window_label=sub_window_label,
            )
            self.budget.record_page_completed(
                path=path, sub_window_label=sub_window_label, page_index=1,
            )
        except TelematicsProviderSafetyError:
            raise
        except Exception:
            return None
        data = payload.get("data")
        if isinstance(data, dict):
            raw = data.get("fuel_consumed")
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None
        if isinstance(data, list) and len(data) > 0:
            raw = data[0].get("fuel_consumed") if isinstance(data[0], dict) else None
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None
        return None

    def fetch_fuel_consumed_batch(
        self,
        registrations: list[str],
        start_timestamp: datetime,
        end_timestamp: datetime,
        sub_window_label: str,
    ) -> List[Dict[str, Any]]:
        """Fetch fuel consumed for up to 100 vehicles over a <=24h period."""
        if len(registrations) > 100:
            raise ValueError("fetch_fuel_consumed_batch supports at most 100 registrations")

        start_utc = _to_utc(start_timestamp)
        end_utc = _to_utc(end_timestamp)
        if end_utc < start_utc:
            raise ValueError("end_timestamp must be >= start_timestamp")
        if end_utc - start_utc > timedelta(hours=24):
            raise ValueError("fetch_fuel_consumed_batch window must be <= 24 hours")

        normalized_registrations: List[str] = []
        for registration in registrations:
            reg = str(registration).strip()
            if not reg:
                raise ValueError("registrations must contain non-empty strings")
            normalized_registrations.append(reg)

        if not normalized_registrations:
            return []

        path = "/fuel/consumed"
        url = f"{self.base_url}{path}"
        body = {
            "registrations": normalized_registrations,
            "start_timestamp": _provider_dt_str(start_utc),
            "end_timestamp": _provider_dt_str(end_utc),
            "page": 1,
            "limit": 100,
        }
        max_attempts = 1 + self.limits.max_retries_per_http_call

        payload: Optional[Dict[str, Any]] = None
        for attempt in range(1, max_attempts + 1):
            self.budget.record_request_issued(path=path, sub_window_label=sub_window_label)
            self._log(
                "INFO",
                "telematics_provider_request",
                {
                    "endpoint": path,
                    "method": "POST",
                    "sub_window": sub_window_label,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "registrations": len(normalized_registrations),
                },
            )
            try:
                r = self._session.post(url, json=body, timeout=self.timeout_s)
                r.raise_for_status()
                parsed = r.json()
                if not isinstance(parsed, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider response JSON is not an object: {path}",
                        context={"endpoint": path, "sub_window": sub_window_label, "type": type(parsed).__name__},
                    )
                payload = parsed
                break
            except TelematicsProviderSafetyError:
                raise
            except (RequestsTimeout, RequestsConnectionError) as e:
                self._log(
                    "WARNING",
                    "telematics_provider_request_retryable",
                    {
                        "endpoint": path,
                        "method": "POST",
                        "sub_window": sub_window_label,
                        "attempt": attempt,
                        "error": type(e).__name__,
                        "detail": str(e),
                    },
                )
                if attempt >= max_attempts:
                    raise TelematicsProviderSafetyError(
                        "HTTP_RETRY_EXHAUSTED",
                        f"Exceeded TELEMATICS_PROVIDER_MAX_RETRIES ({self.limits.max_retries_per_http_call}) for {path}",
                        context={
                            "endpoint": path,
                            "sub_window": sub_window_label,
                            "attempts": attempt,
                            "last_error": type(e).__name__,
                            "detail": str(e),
                        },
                    ) from e
            except RequestsHTTPError as e:
                raise TelematicsProviderSafetyError(
                    "HTTP_ERROR",
                    f"Provider HTTP error: {path} status={getattr(e.response, 'status_code', None)}",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "status_code": getattr(e.response, "status_code", None),
                    },
                ) from e
            except json.JSONDecodeError as e:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider response is not valid JSON: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "detail": str(e)},
                ) from e

        if payload is None:
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Provider response missing after POST: {path}",
                context={"endpoint": path, "sub_window": sub_window_label},
            )

        self.budget.record_page_completed(path=path, sub_window_label=sub_window_label, page_index=1)

        meta_raw = payload.get("meta")
        current_page, last_page = self._parse_pagination_meta(meta_raw, path=path, sub_window_label=sub_window_label)
        if (current_page is None) != (last_page is None):
            raise TelematicsProviderSafetyError(
                "MALFORMED_PAGINATION",
                "Partial pagination meta: current_page and last_page must both be present or both absent",
                context={
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                },
            )
        if current_page is not None and current_page != 1:
            raise TelematicsProviderSafetyError(
                "PAGINATION_MISMATCH",
                "Response meta current_page does not match requested page",
                context={
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "requested_page": 1,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                },
            )
        if last_page is not None and last_page > 1:
            raise TelematicsProviderSafetyError(
                "PAGINATION_NON_PROGRESS",
                "Batch fuel response exceeded one page; caller must choose a smaller registration batch",
                context={"endpoint": path, "sub_window": sub_window_label, "last_page": last_page},
            )

        data = payload.get("data")
        if data is None:
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Missing data key in provider response: {path}",
                context={"endpoint": path, "sub_window": sub_window_label},
            )
        if not isinstance(data, list):
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Provider data is not a list: {path}",
                context={"endpoint": path, "sub_window": sub_window_label},
            )

        def _optional_float(raw: Any) -> Optional[float]:
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        items: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data item is not an object: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "type": type(item).__name__},
                )
            registration = item.get("registration")
            items.append({
                "registration": str(registration).strip() if registration is not None else "",
                "vehicle_id": item.get("vehicle_id"),
                "fuel_consumed_start": _optional_float(item.get("fuel_consumed_start")),
                "fuel_consumed_end": _optional_float(item.get("fuel_consumed_end")),
                "fuel_consumed": _optional_float(item.get("fuel_consumed")),
            })
        return items

    def fetch_fuel_level(
        self,
        registration: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        sub_window_label: str,
    ) -> Dict[str, Any]:
        """Fetch start/end fuel level for one vehicle over a local-day period."""
        reg = str(registration).strip()
        if not reg:
            raise ValueError("registration must be a non-empty string")
        if _to_utc(end_timestamp) < _to_utc(start_timestamp):
            raise ValueError("end_timestamp must be >= start_timestamp")

        path = f"/fuel/level/{reg}"
        params = {
            "start_timestamp": _provider_wall_clock_dt_str(start_timestamp),
            "end_timestamp": _provider_wall_clock_dt_str(end_timestamp),
        }
        payload = self._request_json(path=path, params=params, sub_window_label=sub_window_label)
        self.budget.record_page_completed(path=path, sub_window_label=sub_window_label, page_index=1)

        data = payload.get("data")
        if data is None:
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Missing data key in provider response: {path}",
                context={"endpoint": path, "sub_window": sub_window_label},
            )
        if not isinstance(data, dict):
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Provider data is not an object: {path}",
                context={"endpoint": path, "sub_window": sub_window_label, "type": type(data).__name__},
            )

        def _optional_float(raw: Any) -> Optional[float]:
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
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
            if isinstance(raw, (int, float)):
                return bool(raw)
            return None

        start_period = data.get("start_period")
        end_period = data.get("end_period")
        if start_period is not None and not isinstance(start_period, dict):
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Provider start_period is not an object: {path}",
                context={"endpoint": path, "sub_window": sub_window_label, "type": type(start_period).__name__},
            )
        if end_period is not None and not isinstance(end_period, dict):
            raise TelematicsProviderSafetyError(
                "MALFORMED_RESPONSE",
                f"Provider end_period is not an object: {path}",
                context={"endpoint": path, "sub_window": sub_window_label, "type": type(end_period).__name__},
            )

        start_obj = start_period or {}
        end_obj = end_period or {}
        return {
            "registration": str(data.get("registration") or reg).strip(),
            "start_liters": _optional_float(start_obj.get("liters")),
            "end_liters": _optional_float(end_obj.get("liters")),
            "start_accurate": _optional_bool(start_obj.get("accurate")),
            "end_accurate": _optional_bool(end_obj.get("accurate")),
            "calibrated": _optional_bool(data.get("calibrated")),
            "estimated_fuel_used": _optional_float(data.get("estimated_fuel_used")),
        }

    def fetch_vehicle_events_fleet(
        self,
        start_timestamp: datetime,
        end_timestamp: datetime,
        sub_window_label: str,
        limit: int = 1000,
        max_pages: int = 500,
        log_context: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[int] = None,
        return_stats: bool = False,
    ) -> Any:
        """Fetch fleet-wide raw vehicle telemetry events for one <24h window.

        This intentionally uses the fleet endpoint, not per-registration
        vehicle-event calls. Pagination is bounded by both the provider
        request budget and the explicit per-day `max_pages` cap.
        """
        start_utc = _to_utc(start_timestamp)
        end_utc = _to_utc(end_timestamp)
        if end_utc < start_utc:
            raise ValueError("end_timestamp must be >= start_timestamp")
        if end_utc - start_utc >= VEHICLE_EVENTS_MAX_REQUEST_WINDOW:
            raise ValueError("fetch_vehicle_events_fleet window must be < 24 hours")
        if limit <= 0:
            raise ValueError("limit must be > 0")
        if max_pages <= 0:
            raise ValueError("max_pages must be > 0")

        # `/vehicles/events` addresses events by Europe/Warsaw wall-clock while
        # returning UTC `event_ts`; see the wire-time contract block above and
        # docs/18. `sub_window_label` stays the UTC label so sub-window
        # identity, budget accounting and logs remain comparable.
        wire_start, wire_end, wire_context = vehicle_events_wire_window(start_utc, end_utc)

        path = "/vehicles/events"
        effective_limit = min(limit, 1000)
        if effective_limit != limit:
            self._log(
                "WARNING",
                "telematics_vehicle_events_fleet_limit_capped",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "requested_limit": limit,
                    "effective_limit": effective_limit,
                },
            )
        all_items: List[Dict[str, Any]] = []
        page = 1
        pages_fetched = 0
        meta_total: Optional[int] = None
        meta_last_page: Optional[int] = None
        stopped_by_empty = False
        stopped_by_max_pages = False
        log_extra = dict(log_context or {})

        self._log(
            "INFO",
            "telematics_vehicle_events_request_window",
            {
                "endpoint": path,
                "sub_window": sub_window_label,
                "wire_start_timestamp": wire_start,
                "wire_end_timestamp": wire_end,
                **wire_context,
                **log_extra,
            },
        )

        while True:
            if meta_last_page is not None and page > meta_last_page:
                self._log(
                    "WARNING",
                    "telematics_vehicle_events_fleet_page_beyond_last_page",
                    {
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_last_page": meta_last_page,
                        **log_extra,
                    },
                )
                break

            if pages_fetched >= max_pages:
                stopped_by_max_pages = True
                break

            params = {
                "start_timestamp": wire_start,
                "end_timestamp": wire_end,
                "page": page,
                "limit": effective_limit,
            }
            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
                timeout_s=timeout_s,
            )
            pages_fetched += 1

            page_items = payload.get("data")
            if page_items is None:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )
            if not isinstance(page_items, list):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    context={"endpoint": path, "sub_window": sub_window_label, "page": page},
                )

            meta_raw = payload.get("meta")
            current_page, last_page = self._parse_pagination_meta(
                meta_raw, path=path, sub_window_label=sub_window_label,
            )
            if (current_page is None) != (last_page is None):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    "Partial pagination meta: current_page and last_page must both be present or both absent",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )
            if isinstance(meta_raw, dict):
                raw_total = meta_raw.get("total")
                if raw_total is not None:
                    try:
                        meta_total = int(raw_total)
                    except (TypeError, ValueError):
                        meta_total = None
            if last_page is not None:
                meta_last_page = last_page

            if current_page is not None and current_page != page:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_MISMATCH",
                    "Response meta current_page does not match requested page",
                    context={
                        "endpoint": path,
                        "sub_window": sub_window_label,
                        "requested_page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            self._log(
                "INFO",
                "telematics_vehicle_events_fleet_page",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "requested_page": page,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                    "meta_total": meta_total,
                    "limit": effective_limit,
                    "page_items": len(page_items),
                    **log_extra,
                },
            )

            if len(page_items) == 0:
                stopped_by_empty = True
                break

            for item in page_items:
                if not isinstance(item, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider data item is not an object: {path}",
                        context={"endpoint": path, "sub_window": sub_window_label, "type": type(item).__name__},
                    )
                normalized = _normalize_vehicle_event(item)
                all_items.append(normalized)

            if current_page is not None and last_page is not None and current_page >= last_page:
                break

            page += 1

        if stopped_by_max_pages:
            self._log(
                "WARNING",
                "telematics_vehicle_events_fleet_max_pages_reached",
                {
                    "endpoint": path,
                    "sub_window": sub_window_label,
                    "limit": effective_limit,
                    "max_pages": max_pages,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "stopped_by_max_pages": True,
                    **log_extra,
                },
            )

        self._log(
            "INFO",
            "telematics_vehicle_events_fleet_pagination_summary",
            {
                "endpoint": path,
                "sub_window": sub_window_label,
                "limit": effective_limit,
                "pages_fetched": pages_fetched,
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "rows_returned": len(all_items),
                **log_extra,
            },
        )

        if return_stats:
            return all_items, {
                "pages_fetched": pages_fetched,
                "records_fetched": len(all_items),
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "limit": effective_limit,
            }
        return all_items

    def fetch_vehicle_events_registration(
        self,
        *,
        registration: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        sub_window_label: str,
        limit: int = 1000,
        max_pages: int = 500,
        log_context: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[int] = None,
        return_stats: bool = False,
    ) -> Any:
        """Fetch raw vehicle telemetry events for one registration/window.

        This uses the same documented fleet endpoint as `fetch_vehicle_events_fleet`
        with a `registration` query parameter. It intentionally does not call a
        per-vehicle path endpoint.
        """
        reg = str(registration).strip()
        if not reg:
            raise ValueError("registration must be a non-empty string")

        start_utc = _to_utc(start_timestamp)
        end_utc = _to_utc(end_timestamp)
        if end_utc < start_utc:
            raise ValueError("end_timestamp must be >= start_timestamp")
        if end_utc - start_utc >= VEHICLE_EVENTS_MAX_REQUEST_WINDOW:
            raise ValueError("fetch_vehicle_events_registration window must be < 24 hours")
        if limit <= 0:
            raise ValueError("limit must be > 0")
        if max_pages <= 0:
            raise ValueError("max_pages must be > 0")

        # Same Warsaw wall-clock request contract as the fleet path. The
        # registration fallback must not be left on UTC serialization: it is
        # the path that runs when the fleet path is failing, so a divergence
        # here would silently change which events a degraded run collects.
        wire_start, wire_end, wire_context = vehicle_events_wire_window(start_utc, end_utc)

        path = "/vehicles/events"
        budget_path = "/vehicles/events:registration"
        effective_limit = min(limit, 1000)
        all_items: List[Dict[str, Any]] = []
        page = 1
        pages_fetched = 0
        meta_total: Optional[int] = None
        meta_last_page: Optional[int] = None
        stopped_by_empty = False
        stopped_by_max_pages = False
        log_extra = dict(log_context or {})

        self._log(
            "INFO",
            "telematics_vehicle_events_request_window",
            {
                "endpoint": budget_path,
                "sub_window": sub_window_label,
                "registration": reg,
                "wire_start_timestamp": wire_start,
                "wire_end_timestamp": wire_end,
                **wire_context,
                **log_extra,
            },
        )

        while True:
            if meta_last_page is not None and page > meta_last_page:
                self._log(
                    "WARNING",
                    "telematics_vehicle_events_registration_page_beyond_last_page",
                    {
                        "endpoint": budget_path,
                        "sub_window": sub_window_label,
                        "registration": reg,
                        "requested_page": page,
                        "meta_last_page": meta_last_page,
                        **log_extra,
                    },
                )
                break

            if pages_fetched >= max_pages:
                stopped_by_max_pages = True
                break

            params = {
                "start_timestamp": wire_start,
                "end_timestamp": wire_end,
                "registration": reg,
                "page": page,
                "limit": effective_limit,
            }
            payload = self._request_json(
                path=path,
                params=params,
                sub_window_label=sub_window_label,
                budget_path=budget_path,
                timeout_s=timeout_s,
            )
            pages_fetched += 1

            page_items = payload.get("data")
            if page_items is None:
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Missing data key in provider response: {path}",
                    context={
                        "endpoint": budget_path,
                        "sub_window": sub_window_label,
                        "registration": reg,
                        "page": page,
                    },
                )
            if not isinstance(page_items, list):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_RESPONSE",
                    f"Provider data is not a list: {path}",
                    context={
                        "endpoint": budget_path,
                        "sub_window": sub_window_label,
                        "registration": reg,
                        "page": page,
                    },
                )

            meta_raw = payload.get("meta")
            current_page, last_page = self._parse_pagination_meta(
                meta_raw, path=budget_path, sub_window_label=sub_window_label,
            )
            if (current_page is None) != (last_page is None):
                raise TelematicsProviderSafetyError(
                    "MALFORMED_PAGINATION",
                    "Partial pagination meta: current_page and last_page must both be present or both absent",
                    context={
                        "endpoint": budget_path,
                        "sub_window": sub_window_label,
                        "registration": reg,
                        "page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )
            if isinstance(meta_raw, dict):
                raw_total = meta_raw.get("total")
                if raw_total is not None:
                    try:
                        meta_total = int(raw_total)
                    except (TypeError, ValueError):
                        meta_total = None
            if last_page is not None:
                meta_last_page = last_page

            if current_page is not None and current_page != page:
                raise TelematicsProviderSafetyError(
                    "PAGINATION_MISMATCH",
                    "Response meta current_page does not match requested page",
                    context={
                        "endpoint": budget_path,
                        "sub_window": sub_window_label,
                        "registration": reg,
                        "requested_page": page,
                        "meta_current_page": current_page,
                        "meta_last_page": last_page,
                    },
                )

            self._log(
                "INFO",
                "telematics_vehicle_events_registration_page",
                {
                    "endpoint": budget_path,
                    "sub_window": sub_window_label,
                    "registration": reg,
                    "requested_page": page,
                    "meta_current_page": current_page,
                    "meta_last_page": last_page,
                    "meta_total": meta_total,
                    "limit": effective_limit,
                    "page_items": len(page_items),
                    **log_extra,
                },
            )

            if len(page_items) == 0:
                stopped_by_empty = True
                break

            for item in page_items:
                if not isinstance(item, dict):
                    raise TelematicsProviderSafetyError(
                        "MALFORMED_RESPONSE",
                        f"Provider data item is not an object: {path}",
                        context={
                            "endpoint": budget_path,
                            "sub_window": sub_window_label,
                            "registration": reg,
                            "type": type(item).__name__,
                        },
                    )
                all_items.append(_normalize_vehicle_event(item, default_registration=reg))

            if current_page is not None and last_page is not None and current_page >= last_page:
                break

            page += 1

        if stopped_by_max_pages:
            self._log(
                "WARNING",
                "telematics_vehicle_events_registration_max_pages_reached",
                {
                    "endpoint": budget_path,
                    "sub_window": sub_window_label,
                    "registration": reg,
                    "limit": effective_limit,
                    "max_pages": max_pages,
                    "pages_fetched": pages_fetched,
                    "meta_total": meta_total,
                    "meta_last_page": meta_last_page,
                    "stopped_by_max_pages": True,
                    **log_extra,
                },
            )

        self._log(
            "INFO",
            "telematics_vehicle_events_registration_pagination_summary",
            {
                "endpoint": budget_path,
                "sub_window": sub_window_label,
                "registration": reg,
                "limit": effective_limit,
                "pages_fetched": pages_fetched,
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "rows_returned": len(all_items),
                **log_extra,
            },
        )

        if return_stats:
            return all_items, {
                "pages_fetched": pages_fetched,
                "records_fetched": len(all_items),
                "meta_total": meta_total,
                "meta_last_page": meta_last_page,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_max_pages": stopped_by_max_pages,
                "limit": effective_limit,
            }
        return all_items

    def fetch_notifications(
        self,
        *,
        window_start_ts: datetime,
        window_end_ts: datetime,
    ) -> List[Dict[str, Any]]:
        all_items: List[Dict[str, Any]] = []
        for sub_start, sub_end in iter_31d_windows(window_start_ts, window_end_ts):
            sw = sub_window_label(sub_start, sub_end)
            base_params = {
                "filter[date_from]": _provider_dt_str(sub_start),
                "filter[date_to]": _provider_dt_str(sub_end),
            }
            all_items.extend(
                self._fetch_paginated(
                    path="/alerts/notifications",
                    base_params=base_params,
                    sub_window_label=sw,
                )
            )
        return all_items
