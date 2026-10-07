#!/usr/bin/env python3
"""Independent disk-space guard for the filesystems that can stop the platform.

A full filesystem is the one failure that breaks PostgreSQL, MinIO, backup
creation and every scheduled job simultaneously — and it is entirely predictable
in advance. Production was measured at 74 % used with ~118 GB free against ~7 GB
of nightly backup growth, i.e. a dated outage that nothing was watching for.

Design notes:

  * Two thresholds. `warning` is "act this week", `critical` is "act now".
    Both are expressed as a free-percentage floor *and* a free-bytes floor;
    whichever is breached first wins, because a percentage alone is useless on a
    large disk and bytes alone are useless on a small one.
  * Deduplicated. Repeated scans below a threshold do not re-email: incident
    identity is (mountpoint, severity) and the existing 120-minute cooldown plus
    24-hour reminder interval govern re-notification.
  * Recovery closes the incident, so a freed filesystem stops nagging and a
    later relapse produces a fresh, clearly-new alert.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import platform_db_conn  # noqa: E402
from ops.operational_alert import (  # noqa: E402
    INCIDENT_DISK_SPACE_CRITICAL,
    INCIDENT_DISK_SPACE_WARNING,
    report_operational_failure,
    utcnow,
)

WATCHDOG_NAME = "disk_space_monitor"
COMPONENT = "ops.disk_space_monitor"

DEFAULT_WARNING_PERCENT = 20.0
DEFAULT_CRITICAL_PERCENT = 10.0
DEFAULT_WARNING_FREE_GB = 60.0
DEFAULT_CRITICAL_FREE_GB = 25.0

SEVERITY_OK = "OK"
SEVERITY_WARNING = "THRESHOLD_WARNING"
SEVERITY_CRITICAL = "THRESHOLD_CRITICAL"

_GB = 1024 ** 3


@dataclass(frozen=True)
class Thresholds:
    warning_percent: float = DEFAULT_WARNING_PERCENT
    critical_percent: float = DEFAULT_CRITICAL_PERCENT
    warning_free_bytes: int = int(DEFAULT_WARNING_FREE_GB * _GB)
    critical_free_bytes: int = int(DEFAULT_CRITICAL_FREE_GB * _GB)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Thresholds":
        values = env if env is not None else os.environ

        def number(name: str, default: float) -> float:
            raw = values.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                parsed = float(str(raw).strip())
            except ValueError:
                return default
            return parsed if parsed >= 0 else default

        return cls(
            warning_percent=number("DISK_MONITOR_WARNING_PERCENT", DEFAULT_WARNING_PERCENT),
            critical_percent=number("DISK_MONITOR_CRITICAL_PERCENT", DEFAULT_CRITICAL_PERCENT),
            warning_free_bytes=int(
                number("DISK_MONITOR_WARNING_FREE_GB", DEFAULT_WARNING_FREE_GB) * _GB
            ),
            critical_free_bytes=int(
                number("DISK_MONITOR_CRITICAL_FREE_GB", DEFAULT_CRITICAL_FREE_GB) * _GB
            ),
        )


@dataclass(frozen=True)
class Usage:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def free_percent(self) -> float:
        return (self.free_bytes / self.total_bytes * 100.0) if self.total_bytes else 0.0


def classify(usage: Usage, thresholds: Thresholds) -> str:
    """Critical wins over warning; either floor may trigger independently."""
    if usage.free_percent <= thresholds.critical_percent or (
        usage.free_bytes <= thresholds.critical_free_bytes
    ):
        return SEVERITY_CRITICAL
    if usage.free_percent <= thresholds.warning_percent or (
        usage.free_bytes <= thresholds.warning_free_bytes
    ):
        return SEVERITY_WARNING
    return SEVERITY_OK


def measure(path: Path) -> Usage:
    stat = shutil.disk_usage(path)
    return Usage(
        path=str(path), total_bytes=stat.total, used_bytes=stat.used, free_bytes=stat.free
    )


def default_paths() -> list[Path]:
    """Filesystems whose exhaustion stops the platform."""
    candidates = [REPO_ROOT, REPO_ROOT / "backups", Path("/var/lib/docker")]
    seen: dict[tuple[int, int], Path] = {}
    for candidate in candidates:
        try:
            key = (os.stat(candidate).st_dev, 0)
        except OSError:
            continue
        seen.setdefault(key, candidate)
    return list(seen.values()) or [REPO_ROOT]


# The only incident codes this monitor may resolve, and only for its own subject.
DISK_INCIDENT_CODES = (INCIDENT_DISK_SPACE_WARNING, INCIDENT_DISK_SPACE_CRITICAL)


def _observe(conn, *, subject_key: str, severity: str, detail: dict[str, Any],
             now: datetime, alerted: bool, fingerprint: str | None = None) -> str | None:
    from ops.execution_watchdog import record_observation
    from ops.execution_watchdog import Observation

    observation = Observation(
        subject_key=subject_key,
        verdict=severity if severity != SEVERITY_OK else "OK",
        title="disk space",
        summary="disk space observation",
        component=COMPONENT,
        detail=detail,
        watchdog_name=WATCHDOG_NAME,
    )
    return record_observation(
        conn, observation, now_utc=now, alerted=alerted, fingerprint=fingerprint,
    )


def scan(
    *,
    paths: Sequence[Path] | None = None,
    thresholds: Thresholds | None = None,
    now: datetime | None = None,
    alert: bool = True,
    conn=None,
    usage_reader=measure,
) -> dict[str, Any]:
    """Measure every watched filesystem and alert on threshold breaches."""
    thresholds = thresholds or Thresholds.from_env()
    now = now or utcnow()
    targets = list(paths) if paths else default_paths()
    owns_conn = conn is None
    if conn is None and alert:
        conn = platform_db_conn()

    results: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    recoveries: list[str] = []
    try:
        for target in targets:
            usage = usage_reader(Path(target))
            severity = classify(usage, thresholds)
            subject_key = f"disk:{usage.path}"
            detail = {
                "watchdog": WATCHDOG_NAME,
                "mountpoint": usage.path,
                "total_bytes": usage.total_bytes,
                "used_bytes": usage.used_bytes,
                "free_bytes": usage.free_bytes,
                "free_percent": round(usage.free_percent, 2),
                "warning_percent": thresholds.warning_percent,
                "critical_percent": thresholds.critical_percent,
                "warning_free_bytes": thresholds.warning_free_bytes,
                "critical_free_bytes": thresholds.critical_free_bytes,
                "severity": severity,
            }
            results.append(detail)

            # `--dry-run` must not persist anything, even when a caller supplied
            # its own connection.
            if not alert:
                continue

            from ops.execution_watchdog import resolve_subject_incidents

            if severity == SEVERITY_OK:
                previous = _observe(
                    conn, subject_key=subject_key, severity=severity, detail=detail,
                    now=now, alerted=False,
                ) if conn is not None else None
                if previous is not None and previous != "OK" and conn is not None:
                    # Scoped to this mountpoint's own open incidents. Resolving by
                    # (component, incident_code) closed every other filesystem's
                    # incident too, because they share one component.
                    if resolve_subject_incidents(
                        conn, subject_key=subject_key,
                        incident_codes=DISK_INCIDENT_CODES, now_utc=now,
                    ):
                        recoveries.append(subject_key)
                continue

            critical = severity == SEVERITY_CRITICAL
            result = report_operational_failure(
                incident_code=(
                    INCIDENT_DISK_SPACE_CRITICAL if critical else INCIDENT_DISK_SPACE_WARNING
                ),
                title=(
                    f"{'Critical' if critical else 'Low'} disk space on {usage.path}: "
                    f"{usage.free_bytes / _GB:.1f} GB free ({usage.free_percent:.1f}%)"
                ),
                summary=(
                    f"{usage.path} has {usage.free_bytes / _GB:.1f} GB free "
                    f"({usage.free_percent:.1f}% of {usage.total_bytes / _GB:.1f} GB). "
                    f"Exhausting this filesystem stops PostgreSQL, MinIO, backup creation "
                    f"and every scheduled job at once."
                ),
                component=COMPONENT,
                severity="critical" if critical else "warning",
                subject_type="mountpoint",
                subject_key=usage.path,
                suggested_action=(
                    "Check backup retention (`ops/backup_retention.py`) and journald usage "
                    "(`journalctl --disk-usage`) first; both grow without bound by default."
                ),
                details=detail,
                extra_identity={"severity": severity},
                occurred_at=now,
                now=now,
                conn=conn,
            )
            fingerprint = getattr(result, "fingerprint", None)
            superseded = 0
            if conn is not None:
                # WARNING → CRITICAL on the same filesystem: the warning incident
                # is superseded, not still true alongside the critical one. Only
                # this subject's other fingerprints are closed; the one just
                # opened is kept.
                superseded = resolve_subject_incidents(
                    conn, subject_key=subject_key, incident_codes=DISK_INCIDENT_CODES,
                    now_utc=now, keep=(fingerprint,) if fingerprint else (),
                )
                _observe(
                    conn, subject_key=subject_key, severity=severity, detail=detail,
                    now=now, alerted=True, fingerprint=fingerprint,
                )
            alerts.append(
                {
                    "mountpoint": usage.path,
                    "severity": severity,
                    "email_enqueued": bool(getattr(result, "email_enqueued", False)),
                    "suppression_reason": getattr(result, "suppression_reason", None),
                    "superseded_incidents": superseded,
                }
            )
    finally:
        if owns_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    worst = SEVERITY_OK
    for item in results:
        if item["severity"] == SEVERITY_CRITICAL:
            worst = SEVERITY_CRITICAL
            break
        if item["severity"] == SEVERITY_WARNING:
            worst = SEVERITY_WARNING
    return {
        "schema": "log-platform-disk-space/v1",
        "checked_at": now.isoformat(),
        "worst_severity": worst,
        "filesystems": results,
        "alerts": alerts,
        "recovered": recoveries,
        "operator_action_required": worst != SEVERITY_OK,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Platform disk-space guard")
    parser.add_argument("--path", action="append", type=Path, default=None)
    parser.add_argument(
        "--dry-run", action="store_true", help="Measure and print without creating incidents"
    )
    parser.add_argument(
        "--fail-on-threshold", action="store_true",
        help="Exit non-zero when any filesystem is at or below a threshold",
    )
    args = parser.parse_args(argv)

    report = scan(paths=args.path, alert=not args.dry_run)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.fail_on_threshold and report["operator_action_required"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
