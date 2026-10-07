#!/usr/bin/env python3
"""Scan fleet-wide /vehicles/events for provider-labeled RPM/OVERREV rows.

This is a live diagnostic helper only. It does not write to the client DB and
does not infer violations from numeric rpm thresholds.

Run from repo root:

    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/scan_telematics_vehicle_events_rpm_labels.py \
      --client-id 5f68d5db-6e2d-421d-8248-544640d3de9f \
      --window-start-ts 2026-04-20T00:00:00Z \
      --window-end-ts 2026-04-27T00:00:00Z \
      --event-description-only
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")


from jobs.api.telematics.control_plane import load_client_account_config  # noqa: E402
from jobs.api.telematics.provider_client import (  # noqa: E402
    TelematicsFleetProviderClient,
    _normalize_vehicle_event,
    sub_window_label,
    vehicle_events_wire_window,
)
from jobs.api.telematics.provider_safety import ProviderRunBudget, SafetyLimits  # noqa: E402
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402
from jobs.api.telematics.sync_trips_and_speeding import (  # noqa: E402
    _iter_vehicle_events_fleet_request_windows,
    _normalize_registration,
    _parse_provider_dt,
)


TEXT_FIELDS_OF_INTEREST = (
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
RPM_LABEL_VARIANTS = ("HIGH_RPM", "HIGH RPM", "HIGH-RPM", "RPM", "REV")
OVERREV_LABEL_VARIANTS = ("OVERREV", "OVER_REV", "OVER REV", "OVER-REV")
SECRET_KEY_PARTS = ("password", "secret", "token", "authorization", "api_key", "apikey")


def _parse_runner_ts(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SECRET_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _safe_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_value(key, item) for item in value[:10]]
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "...<truncated>"
    return value


def _provider_log(level: str, message: str, context: dict) -> None:
    if message not in {
        "telematics_provider_page",
        "telematics_vehicle_events_fleet_page",
        "telematics_vehicle_events_fleet_pagination_summary",
        "telematics_vehicle_events_fleet_max_pages_reached",
    }:
        return
    compact = {
        "level": level,
        "message": message,
        "endpoint": context.get("endpoint"),
        "sub_window": context.get("sub_window"),
        "page": context.get("requested_page"),
        "page_items": context.get("page_items"),
        "pages_fetched": context.get("pages_fetched"),
        "rows_returned": context.get("rows_returned"),
        "meta_last_page": context.get("meta_last_page"),
        "stopped_by_max_pages": context.get("stopped_by_max_pages"),
    }
    print(json.dumps(compact, default=str, sort_keys=True), file=sys.stderr, flush=True)


def _flatten_text_values(value: Any, *, prefix: str = "") -> Iterable[Tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_text_values(child, prefix=child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value[:50]):
            yield from _flatten_text_values(child, prefix=f"{prefix}[{idx}]")
    elif isinstance(value, str):
        yield prefix, value


def _matches_any(text: str, variants: Tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(variant.lower() in lowered for variant in variants)


def _label_hits(raw: Dict[str, Any]) -> Tuple[List[Dict[str, str]], bool, bool]:
    hits: List[Dict[str, str]] = []
    rpm_like = False
    overrev_like = False

    for field_path, text in _flatten_text_values(raw):
        field_rpm = _matches_any(text, RPM_LABEL_VARIANTS)
        field_overrev = _matches_any(text, OVERREV_LABEL_VARIANTS)
        if not field_rpm and not field_overrev:
            continue
        rpm_like = rpm_like or field_rpm
        overrev_like = overrev_like or field_overrev
        hits.append({
            "field": field_path,
            "value": str(_safe_value(field_path, text)),
            "rpm_like": str(field_rpm).lower(),
            "overrev_like": str(field_overrev).lower(),
        })
    return hits, rpm_like, overrev_like


def _event_summary(event: Dict[str, Any], *, hits: List[Dict[str, str]]) -> Dict[str, Any]:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return {
        "registration": event.get("registration"),
        "vehicle_id": event.get("vehicle_id"),
        "event_ts": event.get("event_ts").isoformat() if isinstance(event.get("event_ts"), datetime) else str(event.get("event_ts")),
        "speed": event.get("speed"),
        "rpm": event.get("rpm"),
        "event_description": raw.get("event_description"),
        "hits": hits,
        "safe_raw": {str(k): _safe_value(str(k), v) for k, v in raw.items()},
    }


def _minimal_event_summary(event: Dict[str, Any], *, hits: List[Dict[str, str]]) -> Dict[str, Any]:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    return {
        "registration": event.get("registration"),
        "vehicle_id": event.get("vehicle_id"),
        "event_ts": event.get("event_ts").isoformat() if isinstance(event.get("event_ts"), datetime) else str(event.get("event_ts")),
        "speed": event.get("speed"),
        "rpm": event.get("rpm"),
        "event_description": raw.get("event_description"),
        "hits": hits,
    }


def _build_provider(cfg, *, page_limit: int, max_pages_per_day: int, days_count: int) -> TelematicsFleetProviderClient:
    estimated_requests = max(1, (max_pages_per_day * days_count) + 50)
    limits = SafetyLimits(
        max_requests_per_run=estimated_requests,
        max_requests_per_endpoint=estimated_requests,
        max_requests_per_subwindow=max(1, max_pages_per_day + 5),
        max_pages_per_subwindow=max(1, max_pages_per_day + 5),
    )
    return TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=resolve_secret(cfg.provider_basic_auth_password_secret_ref),
        page_limit=page_limit,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        log_fn=_provider_log,
    )


def _fetch_vehicle_events_page(
    provider: TelematicsFleetProviderClient,
    *,
    chunk_start: datetime,
    request_end: datetime,
    page: int,
    limit: int,
    sub_window: str,
) -> Tuple[List[Dict[str, Any]], Optional[int], Optional[int], Optional[int]]:
    # `/vehicles/events` addresses events by Europe/Warsaw wall-clock (proven
    # by the DELTA00001 probe on 2026-08-10, see docs/18). This diagnostic must
    # serialize its window exactly as the production client does, or it reports
    # on a different period than the job reads.
    wire_start, wire_end, _wire_context = vehicle_events_wire_window(chunk_start, request_end)
    payload = provider._request_json(
        path="/vehicles/events",
        params={
            "start_timestamp": wire_start,
            "end_timestamp": wire_end,
            "page": page,
            "limit": limit,
        },
        sub_window_label=sub_window,
    )
    page_items = payload.get("data")
    if page_items is None:
        raise ValueError("Provider response missing data key")
    if not isinstance(page_items, list):
        raise ValueError(f"Provider response data is not a list: {type(page_items).__name__}")
    meta_raw = payload.get("meta")
    current_page, last_page = provider._parse_pagination_meta(
        meta_raw,
        path="/vehicles/events",
        sub_window_label=sub_window,
    )
    meta_total: Optional[int] = None
    if isinstance(meta_raw, dict) and meta_raw.get("total") is not None:
        try:
            meta_total = int(meta_raw.get("total"))
        except (TypeError, ValueError):
            meta_total = None
    rows: List[Dict[str, Any]] = []
    for item in page_items:
        if not isinstance(item, dict):
            raise ValueError(f"Provider data item is not an object: {type(item).__name__}")
        rows.append(_normalize_vehicle_event(item))
    return rows, current_page, last_page, meta_total


def _load_trips(provider: TelematicsFleetProviderClient, *, start_ts: datetime, end_ts: datetime) -> List[Dict[str, Any]]:
    trips: List[Dict[str, Any]] = []
    for raw in provider.fetch_trips(window_start_ts=start_ts, window_end_ts=end_ts):
        start = _parse_provider_dt(raw.get("start_timestamp"))
        end = _parse_provider_dt(raw.get("end_timestamp"))
        if start is None or end is None or end < start:
            continue
        trips.append({
            "provider_trip_id": raw.get("trip_id"),
            "registration": raw.get("registration"),
            "registration_norm": _normalize_registration(raw.get("registration")),
            "vehicle_id": raw.get("vehicle_id"),
            "start_ts": start,
            "end_ts": end,
        })
    return trips


def _build_trip_indexes(trips: List[Dict[str, Any]]) -> Tuple[Dict[Any, List[Dict[str, Any]]], Dict[str, List[Dict[str, Any]]]]:
    by_vehicle: Dict[Any, List[Dict[str, Any]]] = {}
    by_registration: Dict[str, List[Dict[str, Any]]] = {}
    for trip in trips:
        vehicle_id = trip.get("vehicle_id")
        if vehicle_id is not None:
            by_vehicle.setdefault(vehicle_id, []).append(trip)
        registration = trip.get("registration_norm")
        if registration:
            by_registration.setdefault(registration, []).append(trip)
    return by_vehicle, by_registration


def _event_matches_trip(
    event: Dict[str, Any],
    *,
    trips_by_vehicle: Dict[Any, List[Dict[str, Any]]],
    trips_by_registration: Dict[str, List[Dict[str, Any]]],
) -> bool:
    event_ts = event.get("event_ts")
    if not isinstance(event_ts, datetime):
        return False
    event_reg = _normalize_registration(event.get("registration"))
    event_vid = event.get("vehicle_id")

    candidates: List[Dict[str, Any]] = []
    seen_trip_ids: set = set()
    if event_vid is not None:
        for trip in trips_by_vehicle.get(event_vid, ()):
            tid = trip.get("provider_trip_id")
            seen_trip_ids.add(tid)
            candidates.append(trip)
    if event_reg:
        for trip in trips_by_registration.get(event_reg, ()):
            tid = trip.get("provider_trip_id")
            if tid in seen_trip_ids:
                continue
            candidates.append(trip)

    for trip in candidates:
        if not (trip["start_ts"] <= event_ts <= trip["end_ts"]):
            continue
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan fleet-wide /vehicles/events for RPM/OVERREV labels.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--window-start-ts", required=True)
    parser.add_argument("--window-end-ts", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-pages-per-day", type=int, default=3)
    parser.add_argument("--max-days", type=int, default=2)
    parser.add_argument("--start-day-offset", type=int, default=0)
    parser.add_argument("--sample-every-n-pages", type=int)
    parser.add_argument("--event-description-only", action="store_true")
    parser.add_argument("--target-registration")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--output", help="Optional path to write JSON report.")
    args = parser.parse_args()

    if args.limit <= 0 or args.limit > 1000:
        raise ValueError("--limit must be between 1 and 1000")
    if args.max_pages_per_day <= 0:
        raise ValueError("--max-pages-per-day must be > 0")
    if args.max_days <= 0:
        raise ValueError("--max-days must be > 0")
    if args.start_day_offset < 0:
        raise ValueError("--start-day-offset must be >= 0")
    if args.sample_every_n_pages is not None and args.sample_every_n_pages <= 0:
        raise ValueError("--sample-every-n-pages must be > 0 when provided")
    if args.sample_size < 0:
        raise ValueError("--sample-size must be >= 0")

    start_ts = _parse_runner_ts(args.window_start_ts)
    end_ts = _parse_runner_ts(args.window_end_ts)
    all_windows = list(_iter_vehicle_events_fleet_request_windows(start_ts, end_ts))
    windows = all_windows[args.start_day_offset:args.start_day_offset + args.max_days]
    target_registration_norm = _normalize_registration(args.target_registration)

    cfg = load_client_account_config(client_id=args.client_id)
    provider = _build_provider(
        cfg,
        page_limit=args.limit,
        max_pages_per_day=args.max_pages_per_day,
        days_count=max(1, len(windows)),
    )
    trips_loaded = False
    trips: List[Dict[str, Any]] = []
    trips_by_vehicle: Dict[Any, List[Dict[str, Any]]] = {}
    trips_by_registration: Dict[str, List[Dict[str, Any]]] = {}

    total_rows = 0
    rpm_like_rows = 0
    overrev_like_rows = 0
    matched_to_trips = 0
    unmatched_to_trips = 0
    event_description_counts: Counter[str] = Counter()
    match_event_description_counts: Counter[str] = Counter()
    matched_rows: List[Dict[str, Any]] = []
    per_window: List[Dict[str, Any]] = []
    stopped_reasons: List[str] = []
    started_at = datetime.now(timezone.utc)

    for local_day_index, (chunk_start, request_end, original_chunk_end) in enumerate(windows, start=1):
        day_index = args.start_day_offset + local_day_index
        sw = sub_window_label(chunk_start, request_end)
        print(
            json.dumps({
                "day": day_index,
                "local_day": local_day_index,
                "progress": "scan_day_start",
                "chunk_start": chunk_start.isoformat(),
                "request_end": request_end.isoformat(),
                "limit": args.limit,
                "max_pages_per_day": args.max_pages_per_day,
                "event_description_only": args.event_description_only,
                "target_registration": args.target_registration,
            }, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        page = 1
        pages_scanned = 0
        stopped_by_max_pages = False
        stopped_by_empty = False
        stopped_by_last_page = False
        meta_last_page: Optional[int] = None
        meta_total: Optional[int] = None
        window_total = 0
        window_rpm = 0
        window_overrev = 0
        window_assigned = 0

        while True:
            if pages_scanned >= args.max_pages_per_day:
                stopped_by_max_pages = True
                stopped_reasons.append(f"day_{day_index}_max_pages_per_day")
                break
            rows, current_page, last_page, page_meta_total = _fetch_vehicle_events_page(
                provider,
                chunk_start=chunk_start,
                request_end=request_end,
                page=page,
                limit=args.limit,
                sub_window=f"diagnostic:rpm_label_scan:{sw}",
            )
            pages_scanned += 1
            meta_last_page = last_page
            if page_meta_total is not None:
                meta_total = page_meta_total

            if not rows:
                stopped_by_empty = True
                break

            for event in rows:
                if target_registration_norm and _normalize_registration(event.get("registration")) != target_registration_norm:
                    continue
                total_rows += 1
                window_total += 1
                raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
                event_description = str(raw.get("event_description") or "<missing>")
                event_description_counts[event_description] += 1

                if args.event_description_only:
                    hits, rpm_like, overrev_like = _label_hits({"event_description": raw.get("event_description")})
                else:
                    hits, rpm_like, overrev_like = _label_hits(raw)
                if not rpm_like and not overrev_like:
                    continue

                window_rpm += 1 if rpm_like else 0
                window_overrev += 1 if overrev_like else 0
                rpm_like_rows += 1 if rpm_like else 0
                overrev_like_rows += 1 if overrev_like else 0
                match_event_description_counts[event_description] += 1

                assigned: Optional[bool] = None
                if not args.event_description_only:
                    if not trips_loaded:
                        trips = _load_trips(provider, start_ts=start_ts, end_ts=end_ts)
                        trips_by_vehicle, trips_by_registration = _build_trip_indexes(trips)
                        trips_loaded = True
                    assigned = _event_matches_trip(
                        event,
                        trips_by_vehicle=trips_by_vehicle,
                        trips_by_registration=trips_by_registration,
                    )
                    if assigned:
                        matched_to_trips += 1
                        window_assigned += 1
                    else:
                        unmatched_to_trips += 1

                if len(matched_rows) < args.sample_size:
                    sample = _event_summary(event, hits=hits)
                    sample["would_assign_to_trip"] = assigned
                    matched_rows.append(sample)

            elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
            progress = {
                "progress": "page_done",
                "day": day_index,
                "local_day": local_day_index,
                "page": page,
                "rows_scanned": total_rows,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "distinct_event_description_count": len(event_description_counts),
                "rpm_like_rows": rpm_like_rows,
                "overrev_like_rows": overrev_like_rows,
                "page_rows_after_target_filter": window_total,
                "meta_last_page": meta_last_page,
                "meta_total": meta_total,
            }
            if args.sample_every_n_pages and page % args.sample_every_n_pages == 0:
                progress["top_event_descriptions"] = dict(event_description_counts.most_common(10))
            print(json.dumps(progress, sort_keys=True), file=sys.stderr, flush=True)

            if current_page is not None and last_page is not None and current_page >= last_page:
                stopped_by_last_page = True
                break
            page += 1

        per_window.append({
            "chunk_start": chunk_start.isoformat(),
            "request_end": request_end.isoformat(),
            "original_chunk_end": original_chunk_end.isoformat(),
            "rows_scanned": window_total,
            "pages_scanned": pages_scanned,
            "meta_last_page": meta_last_page,
            "meta_total": meta_total,
            "stopped_by_max_pages": stopped_by_max_pages,
            "stopped_by_empty": stopped_by_empty,
            "stopped_by_last_page": stopped_by_last_page,
            "rpm_like_rows": window_rpm,
            "overrev_like_rows": window_overrev,
            "matched_label_rows_assigned_to_trips": window_assigned,
        })
        print(
            json.dumps({
                "progress": "scan_vehicle_events_day_done",
                "day": day_index,
                "local_day": local_day_index,
                "chunk_start": chunk_start.isoformat(),
                "rows_scanned": window_total,
                "pages_scanned": pages_scanned,
                "stopped_by_max_pages": stopped_by_max_pages,
                "stopped_by_empty": stopped_by_empty,
                "stopped_by_last_page": stopped_by_last_page,
                "rpm_like_rows": window_rpm,
                "overrev_like_rows": window_overrev,
                "matched_label_rows_assigned_to_trips": window_assigned,
            }, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )

    next_start_day_offset = args.start_day_offset + len(windows)
    if next_start_day_offset < len(all_windows):
        stopped_reasons.append("max_days")

    report = {
        "client_id": args.client_id,
        "endpoint": "/vehicles/events",
        "window_start_ts": start_ts.isoformat(),
        "window_end_ts": end_ts.isoformat(),
        "limit": args.limit,
        "max_pages_per_day": args.max_pages_per_day,
        "max_days": args.max_days,
        "start_day_offset": args.start_day_offset,
        "sample_every_n_pages": args.sample_every_n_pages,
        "event_description_only": args.event_description_only,
        "target_registration": args.target_registration,
        "total_days_in_requested_window": len(all_windows),
        "days_scanned": len(windows),
        "next_start_day_offset": next_start_day_offset if next_start_day_offset < len(all_windows) else None,
        "trips_loaded": len(trips) if trips_loaded else 0,
        "trip_assignment_performed": trips_loaded,
        "stopped_due_to_safety_limits": bool(stopped_reasons),
        "stopped_reasons": stopped_reasons,
        "total_rows_scanned": total_rows,
        "rows_matching_rpm_like_labels": rpm_like_rows,
        "rows_matching_overrev_like_labels": overrev_like_rows,
        "label_rows_assigned_to_trips": matched_to_trips,
        "label_rows_unmatched_to_trips": unmatched_to_trips,
        "distinct_event_description_values_with_counts": dict(event_description_counts.most_common()),
        "matched_event_description_values_with_counts": dict(match_event_description_counts.most_common()),
        "per_window": per_window,
        "safe_sample_rows_for_matches": matched_rows,
        "label_variants_searched": {
            "rpm_like": list(RPM_LABEL_VARIANTS),
            "overrev_like": list(OVERREV_LABEL_VARIANTS),
        },
        "text_fields_of_interest": list(TEXT_FIELDS_OF_INTEREST),
        "note": "No numeric rpm threshold inference is performed; matches are provider text-label only.",
    }

    text = json.dumps(report, default=str, indent=2, sort_keys=True)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
