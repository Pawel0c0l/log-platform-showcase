#!/usr/bin/env python3
"""Probe Telematics GET /vehicles/events with bounded limit/window variants.

This is a live diagnostic helper, not an offline unit test. It loads the same
Workflow A client account config as the sync job and requests only page 1
(`max_pages=1`) for each probe, stopping at the first 200/success per range.

Example:

    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/diagnose_telematics_vehicle_events.py \
      --client-id 5f68d5db-6e2d-421d-8248-544640d3de9f
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List


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
    provider_page_limit_from_env,
)
from jobs.api.telematics.provider_safety import (  # noqa: E402
    TelematicsProviderSafetyError,
    SafetyLimits,
)
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402


DEFAULT_LIMITS = [2000, 1000, 500, 100, 50, 10]
DEFAULT_SHORT_LIMITS = [100, 10]


def _parse_provider_ts(raw: str) -> datetime:
    raw = raw.strip()
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_limits(raw: str) -> List[int]:
    limits: List[int] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        limit = int(value)
        if limit <= 0:
            raise ValueError("limits must be positive integers")
        limits.append(limit)
    if not limits:
        raise ValueError("at least one limit is required")
    return limits


def _log(level: str, message: str, context: dict) -> None:
    print(json.dumps({"level": level, "message": message, "context": context}, default=str, sort_keys=True))


def _probe_range(
    *,
    client: TelematicsFleetProviderClient,
    label: str,
    start_ts: datetime,
    end_ts: datetime,
    limits: Iterable[int],
) -> bool:
    print(f"\nProbe range {label}: {start_ts.isoformat()} -> {end_ts.isoformat()}")
    for limit in limits:
        try:
            rows = client.fetch_vehicle_events_fleet(
                start_timestamp=start_ts,
                end_timestamp=end_ts,
                sub_window_label=f"diagnostic:{label}:limit={limit}",
                limit=limit,
                max_pages=1,
            )
        except TelematicsProviderSafetyError as exc:
            print(json.dumps({
                "label": label,
                "limit": limit,
                "ok": False,
                "code": exc.code,
                "context": exc.context,
            }, default=str, sort_keys=True))
            continue

        print(json.dumps({
            "label": label,
            "limit": limit,
            "ok": True,
            "rows_returned_page_1": len(rows),
        }, sort_keys=True))
        return True

    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Telematics /vehicles/events 422 behavior.")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--start", default="2026-04-26 00:00:00")
    parser.add_argument("--end", default="2026-04-26 23:59:59")
    parser.add_argument("--limits", default=",".join(str(v) for v in DEFAULT_LIMITS))
    parser.add_argument("--short-start", default="2026-04-26 00:00:00")
    parser.add_argument("--short-end", default="2026-04-26 00:10:00")
    parser.add_argument("--short-limits", default=",".join(str(v) for v in DEFAULT_SHORT_LIMITS))
    args = parser.parse_args()

    cfg = load_client_account_config(client_id=args.client_id)
    client = TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=resolve_secret(cfg.provider_basic_auth_password_secret_ref),
        page_limit=provider_page_limit_from_env(),
        safety_limits=SafetyLimits(),
        log_fn=_log,
    )

    main_ok = _probe_range(
        client=client,
        label="daily",
        start_ts=_parse_provider_ts(args.start),
        end_ts=_parse_provider_ts(args.end),
        limits=_parse_limits(args.limits),
    )
    short_ok = _probe_range(
        client=client,
        label="short",
        start_ts=_parse_provider_ts(args.short_start),
        end_ts=_parse_provider_ts(args.short_end),
        limits=_parse_limits(args.short_limits),
    )

    if not main_ok and not short_ok:
        print("No /vehicles/events probe succeeded. Check emitted HTTP_ERROR context response_body_text/headers.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
