#!/usr/bin/env python3
"""Inspect raw Telematics /vehicles/events rows for RPM/OVERREV signals.

This is a live diagnostic helper, not production sync logic. It loads the
same Workflow A client account config as the sync job, fetches a bounded
sample from fleet-wide GET /vehicles/events, and prints a safe structural
summary of raw event rows.

Run from repo root:

    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/inspect_telematics_vehicle_events_fields.py \
      --client-id <uuid> \
      --start "2026-04-26 00:00:00" \
      --end "2026-04-26 00:10:00"
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


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
from jobs.api.telematics.provider_client import TelematicsFleetProviderClient, provider_page_limit_from_env  # noqa: E402
from jobs.api.telematics.provider_safety import TelematicsProviderSafetyError, SafetyLimits  # noqa: E402
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402


TYPE_FIELDS = (
    "type",
    "event_type",
    "event_description",
    "description",
    "trigger_description",
    "alert_type",
    "notification_type",
)
RPM_NUMERIC_FIELDS = ("rpm", "engine_rpm", "engine_speed", "value", "reading")
KEYWORDS = ("HIGH_RPM", "RPM", "OVERREV", "OVER_REV", "engine", "rev", "obroty")
SECRET_KEY_PARTS = ("password", "secret", "token", "authorization", "api_key", "apikey")


def _parse_ts(raw: str) -> datetime:
    value = raw.strip()
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _redact_or_shorten(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SECRET_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_or_shorten(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_or_shorten(key, item) for item in value[:10]]
    if isinstance(value, str):
        return value if len(value) <= 160 else value[:160] + "...<truncated>"
    return value


def _flatten_values(value: Any, *, prefix: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_values(child, prefix=child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value[:25]):
            yield from _flatten_values(child, prefix=f"{prefix}[{idx}]")
    else:
        yield prefix, value


def _log(level: str, message: str, context: dict) -> None:
    print(json.dumps({"level": level, "message": message, "context": context}, default=str, sort_keys=True))


def _summarize(rows: List[Dict[str, Any]], *, sample_size: int) -> Dict[str, Any]:
    raw_rows = [row.get("raw") for row in rows if isinstance(row.get("raw"), dict)]

    top_level_keys: Counter[str] = Counter()
    type_values: Dict[str, Counter[str]] = {field: Counter() for field in TYPE_FIELDS}
    keyword_hits: Dict[str, Dict[str, Any]] = {
        keyword: {"count": 0, "fields": Counter(), "samples": []}
        for keyword in KEYWORDS
    }
    numeric_stats: Dict[str, Dict[str, Any]] = {
        field: {"present": 0, "numeric": 0, "min": None, "max": None, "samples": []}
        for field in RPM_NUMERIC_FIELDS
    }
    assignment_fields = {
        "registration_present": 0,
        "vehicle_id_present": 0,
        "event_ts_present": 0,
    }

    for raw in raw_rows:
        for key in raw.keys():
            top_level_keys[str(key)] += 1

        if raw.get("registration") not in (None, ""):
            assignment_fields["registration_present"] += 1
        if raw.get("vehicle_id") not in (None, ""):
            assignment_fields["vehicle_id_present"] += 1
        if raw.get("event_ts") not in (None, ""):
            assignment_fields["event_ts_present"] += 1

        for field in TYPE_FIELDS:
            value = raw.get(field)
            if value not in (None, ""):
                type_values[field][str(value).strip()] += 1

        for field in RPM_NUMERIC_FIELDS:
            value = raw.get(field)
            if value in (None, ""):
                continue
            stats = numeric_stats[field]
            stats["present"] += 1
            if len(stats["samples"]) < 10:
                stats["samples"].append(_redact_or_shorten(field, value))
            number = _as_number(value)
            if number is None:
                continue
            stats["numeric"] += 1
            stats["min"] = number if stats["min"] is None else min(stats["min"], number)
            stats["max"] = number if stats["max"] is None else max(stats["max"], number)

        for field_path, value in _flatten_values(raw):
            if value is None:
                continue
            text = str(value)
            text_lower = text.lower()
            for keyword in KEYWORDS:
                if keyword.lower() not in text_lower:
                    continue
                hit = keyword_hits[keyword]
                hit["count"] += 1
                hit["fields"][field_path] += 1
                if len(hit["samples"]) < 10:
                    hit["samples"].append({
                        "field": field_path,
                        "value": _redact_or_shorten(field_path, text),
                    })

    keyword_summary = {}
    for keyword, hit in keyword_hits.items():
        keyword_summary[keyword] = {
            "count": hit["count"],
            "fields": dict(hit["fields"].most_common(20)),
            "samples": hit["samples"],
        }

    return {
        "rows_returned": len(rows),
        "raw_rows_seen": len(raw_rows),
        "top_level_keys": sorted(top_level_keys.keys()),
        "top_level_key_counts": dict(top_level_keys.most_common()),
        "distinct_type_field_values": {
            field: dict(counter.most_common(25))
            for field, counter in type_values.items()
        },
        "keyword_hits": keyword_summary,
        "rpm_like_numeric_fields": numeric_stats,
        "assignment_fields": assignment_fields,
        "sample_rows": [
            {str(k): _redact_or_shorten(str(k), v) for k, v in raw.items()}
            for raw in raw_rows[:sample_size]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Telematics /vehicles/events raw fields.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--start", required=True, help='UTC start, e.g. "2026-04-26 00:00:00"')
    parser.add_argument("--end", required=True, help='UTC end, must be <24h after start')
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--sample-size", type=int, default=5)
    args = parser.parse_args()

    if args.limit <= 0:
        raise ValueError("--limit must be > 0")
    if args.max_pages <= 0:
        raise ValueError("--max-pages must be > 0")
    if args.sample_size < 0:
        raise ValueError("--sample-size must be >= 0")

    start_ts = _parse_ts(args.start)
    end_ts = _parse_ts(args.end)

    cfg = load_client_account_config(client_id=args.client_id)
    provider = TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=resolve_secret(cfg.provider_basic_auth_password_secret_ref),
        page_limit=provider_page_limit_from_env(),
        safety_limits=SafetyLimits(),
        log_fn=_log,
    )

    try:
        rows = provider.fetch_vehicle_events_fleet(
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            sub_window_label=f"diagnostic:vehicle_events_fields:{start_ts.isoformat()}..{end_ts.isoformat()}",
            limit=args.limit,
            max_pages=args.max_pages,
        )
    except TelematicsProviderSafetyError as exc:
        print(json.dumps({
            "ok": False,
            "code": exc.code,
            "context": exc.context,
        }, default=str, sort_keys=True))
        return 2

    print(json.dumps({
        "ok": True,
        "client_id": args.client_id,
        "endpoint": "/vehicles/events",
        "start": start_ts.isoformat(),
        "end": end_ts.isoformat(),
        "limit": args.limit,
        "max_pages": args.max_pages,
        "summary": _summarize(rows, sample_size=args.sample_size),
    }, default=str, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
