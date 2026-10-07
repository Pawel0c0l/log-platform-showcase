#!/usr/bin/env python3
"""Inspect Telematics GET /alerts/notifications for RPM/OVERREV labels.

This is a live diagnostic helper, not production sync logic. It loads the
same Workflow A client account config as the sync job, fetches bounded raw
notification samples, and prints safe structural summaries. It probes both
the OpenAPI-documented bracketed filter parameter style and the plain
parameter style for comparison. It can optionally probe alert-type filters
for HIGH_RPM and OVERREV.

Run from repo root:

    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/diagnose_telematics_notifications.py \
      --client-id <uuid> \
      --window-start-ts "2026-04-26T00:00:00Z" \
      --window-end-ts "2026-04-27T00:00:00Z" \
      --try-alert-type-filters
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
from jobs.api.telematics.provider_client import (  # noqa: E402
    TelematicsFleetProviderClient,
    _provider_dt_str,
    iter_31d_windows,
    provider_page_limit_from_env,
    sub_window_label,
)
from jobs.api.telematics.provider_safety import (  # noqa: E402
    TelematicsProviderSafetyError,
    ProviderRunBudget,
    SafetyLimits,
)
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402
from jobs.api.telematics.sync_trips_and_speeding import _extract_notification_type  # noqa: E402


TYPE_FIELDS = (
    "trigger_description",
    "type",
    "notification_type",
    "event_type",
    "alert_type",
    "event_description",
    "description",
)
KEYWORDS = ("HIGH_RPM", "HIGH RPM", "RPM", "OVERREV", "OVER_REV", "rev", "obroty")
SECRET_KEY_PARTS = ("password", "secret", "token", "authorization", "api_key", "apikey")
PARAM_STYLE_DOCUMENTED = "documented_filter"
PARAM_STYLE_PLAIN = "plain"


def _parse_runner_ts(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _redact_or_shorten(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in SECRET_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_or_shorten(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_or_shorten(key, item) for item in value[:10]]
    if isinstance(value, str):
        return value if len(value) <= 200 else value[:200] + "...<truncated>"
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


def _fetch_notifications_bounded(
    provider: TelematicsFleetProviderClient,
    *,
    window_start_ts: datetime,
    window_end_ts: datetime,
    alert_type_filter: Optional[str] = None,
    param_style: str = PARAM_STYLE_DOCUMENTED,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sub_start, sub_end in iter_31d_windows(window_start_ts, window_end_ts):
        sw = sub_window_label(sub_start, sub_end)
        if param_style == PARAM_STYLE_DOCUMENTED:
            date_from_key = "filter[date_from]"
            date_to_key = "filter[date_to]"
            alert_type_key = "filter[alert_type]"
        elif param_style == PARAM_STYLE_PLAIN:
            date_from_key = "date_from"
            date_to_key = "date_to"
            alert_type_key = "alert_type"
        else:
            raise ValueError(f"Unsupported param_style: {param_style}")

        base_params = {
            date_from_key: _provider_dt_str(sub_start),
            date_to_key: _provider_dt_str(sub_end),
        }
        label = sw
        if alert_type_filter:
            base_params[alert_type_key] = alert_type_filter
            label = f"{sw}:{alert_type_key}={alert_type_filter}"
        rows.extend(
            provider._fetch_paginated(
                path="/alerts/notifications",
                base_params=base_params,
                sub_window_label=f"diagnostic:alerts_notifications:{param_style}:{label}",
            )
        )
    return rows


def _summarize(rows: List[Dict[str, Any]], *, sample_size: int) -> Dict[str, Any]:
    key_counts: Counter[str] = Counter()
    type_values: Dict[str, Counter[str]] = {field: Counter() for field in TYPE_FIELDS}
    notification_msg_keyword_matches: Counter[str] = Counter()
    canonical_type_counts: Counter[str] = Counter()
    keyword_hits: Dict[str, Dict[str, Any]] = {
        keyword: {"count": 0, "fields": Counter(), "samples": []}
        for keyword in KEYWORDS
    }

    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            key_counts[str(key)] += 1

        canonical = _extract_notification_type(row)
        if canonical is None:
            canonical_type_counts["<missing>"] += 1
        else:
            canonical_type_counts[canonical] += 1

        for field in TYPE_FIELDS:
            value = row.get(field)
            if value not in (None, ""):
                type_values[field][str(value).strip()] += 1

        msg = row.get("notification_msg")
        if msg not in (None, ""):
            msg_text = str(msg).lower()
            for keyword in KEYWORDS:
                if keyword.lower() in msg_text:
                    notification_msg_keyword_matches[keyword] += 1

        for field_path, value in _flatten_values(row):
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

    return {
        "rows_returned": len(rows),
        "top_level_keys": sorted(key_counts.keys()),
        "top_level_key_counts": dict(key_counts.most_common()),
        "canonical_type_counts_with_current_parser": dict(canonical_type_counts.most_common()),
        "distinct_type_field_values": {
            field: dict(counter.most_common(30))
            for field, counter in type_values.items()
        },
        "notification_msg_keyword_matches": dict(notification_msg_keyword_matches.most_common()),
        "keyword_hits_any_safe_text_field": {
            keyword: {
                "count": hit["count"],
                "fields": dict(hit["fields"].most_common(20)),
                "samples": hit["samples"],
            }
            for keyword, hit in keyword_hits.items()
        },
        "safe_sample_rows": [
            {str(k): _redact_or_shorten(str(k), v) for k, v in row.items()}
            for row in rows[:sample_size]
            if isinstance(row, dict)
        ],
    }


def _build_provider(cfg, *, page_limit: int, max_pages_per_window: int) -> TelematicsFleetProviderClient:
    limits = SafetyLimits(
        max_requests_per_run=max(1, max_pages_per_window * 6),
        max_requests_per_endpoint=max(1, max_pages_per_window * 6),
        max_requests_per_subwindow=max(1, max_pages_per_window),
        max_pages_per_subwindow=max(1, max_pages_per_window),
    )
    return TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=resolve_secret(cfg.provider_basic_auth_password_secret_ref),
        page_limit=page_limit,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        log_fn=_log,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Telematics /alerts/notifications RPM labels.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--window-start-ts", required=True)
    parser.add_argument("--window-end-ts", required=True)
    parser.add_argument("--page-limit", type=int, default=provider_page_limit_from_env())
    parser.add_argument("--max-pages-per-window", type=int, default=2)
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--try-alert-type-filters", action="store_true")
    parser.add_argument("--output", help="Optional path to write the same JSON report.")
    args = parser.parse_args()

    if args.page_limit <= 0:
        raise ValueError("--page-limit must be > 0")
    if args.max_pages_per_window <= 0:
        raise ValueError("--max-pages-per-window must be > 0")
    if args.sample_size < 0:
        raise ValueError("--sample-size must be >= 0")

    window_start_ts = _parse_runner_ts(args.window_start_ts)
    window_end_ts = _parse_runner_ts(args.window_end_ts)
    cfg = load_client_account_config(client_id=args.client_id)

    report: Dict[str, Any] = {
        "client_id": args.client_id,
        "endpoint": "/alerts/notifications",
        "window_start_ts": window_start_ts.isoformat(),
        "window_end_ts": window_end_ts.isoformat(),
        "page_limit": args.page_limit,
        "max_pages_per_window": args.max_pages_per_window,
        "documented_query_parameters": {
            "date_from": "filter[date_from]",
            "date_to": "filter[date_to]",
            "registration": "filter[registration]",
            "alert_type": "filter[alert_type]",
            "page": "page",
            "limit": "limit",
        },
        "parameter_style_probes": {},
    }

    for param_style in (PARAM_STYLE_DOCUMENTED, PARAM_STYLE_PLAIN):
        style_report: Dict[str, Any] = {"unfiltered": {}, "alert_type_filter_probes": {}}
        try:
            provider = _build_provider(
                cfg,
                page_limit=args.page_limit,
                max_pages_per_window=args.max_pages_per_window,
            )
            rows = _fetch_notifications_bounded(
                provider,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                param_style=param_style,
            )
            style_report["unfiltered"] = _summarize(rows, sample_size=args.sample_size)
        except TelematicsProviderSafetyError as exc:
            style_report["unfiltered"] = {"ok": False, "code": exc.code, "context": exc.context}

        if args.try_alert_type_filters:
            for alert_type in ("HIGH_RPM", "OVERREV"):
                try:
                    provider = _build_provider(
                        cfg,
                        page_limit=args.page_limit,
                        max_pages_per_window=args.max_pages_per_window,
                    )
                    filtered_rows = _fetch_notifications_bounded(
                        provider,
                        window_start_ts=window_start_ts,
                        window_end_ts=window_end_ts,
                        alert_type_filter=alert_type,
                        param_style=param_style,
                    )
                    style_report["alert_type_filter_probes"][alert_type] = _summarize(
                        filtered_rows,
                        sample_size=args.sample_size,
                    )
                except TelematicsProviderSafetyError as exc:
                    style_report["alert_type_filter_probes"][alert_type] = {
                        "ok": False,
                        "code": exc.code,
                        "context": exc.context,
                    }
        report["parameter_style_probes"][param_style] = style_report

    row_count_summary: Dict[str, Dict[str, int]] = {}
    for param_style, style_report in report["parameter_style_probes"].items():
        row_count_summary[param_style] = {
            "unfiltered": int(
                ((style_report.get("unfiltered") or {}).get("rows_returned") or 0)
            )
        }
        for alert_type, probe in (style_report.get("alert_type_filter_probes") or {}).items():
            row_count_summary[param_style][f"alert_type={alert_type}"] = int(
                (probe.get("rows_returned") or 0)
            )
    report["row_count_summary_by_parameter_style"] = row_count_summary

    # Keep backward-compatible top-level aliases for quick visual inspection.
    documented_probe = report["parameter_style_probes"].get(PARAM_STYLE_DOCUMENTED, {})
    report["unfiltered"] = documented_probe.get("unfiltered", {})
    report["alert_type_filter_probes"] = documented_probe.get("alert_type_filter_probes", {})

    text = json.dumps(report, default=str, indent=2, sort_keys=True)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
