#!/usr/bin/env python3
"""systemd `OnFailure=` adapter: turn a failed unit into an operator incident.

Some failures never reach application code at all. On 2026-07-31 20:00 and
2026-08-01 06:00 `log-workflow-b.service` exited non-zero *before* it could
create a `public.runs` row; the only trace was journald, which has since
rotated. An in-process error handler cannot report a process that never got far
enough to have one.

systemd can. `OnFailure=` fires from PID 1, independently of whatever died, so
this adapter is invoked with the failed unit name and records a durable incident
directly against PostgreSQL — deliberately not through the platform HTTP API,
which may itself be the thing that is down.

Anti-recursion is the load-bearing property here:

  * the alert email worker is never reported through this adapter — a broken
    mail path must surface as outbox `dead_letter` state and a failed unit, not
    by asking itself to send another email;
  * this adapter carries no `OnFailure=` of its own, so it cannot trigger itself;
  * refusal is logged as an ordinary operational error on stderr, which is where
    an operator inspecting a broken alert path will already be looking.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.operational_alert import (  # noqa: E402
    INCIDENT_UNIT_FAILURE,
    is_self_alerting,
    report_operational_failure,
    utcnow,
)

COMPONENT_PREFIX = "systemd.unit"

# Units whose failure must never be routed back through the alert path.
NEVER_REPORT = frozenset(
    {
        "suspected-bug-email-worker.service",
        "log-platform-unit-failure@.service",
    }
)


def _unit_property(unit: str, name: str) -> str | None:
    """Best-effort systemd property read. Absent systemctl is not an error."""
    try:
        completed = subprocess.run(
            ["systemctl", "show", unit, "--property", name, "--value"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (completed.stdout or "").strip()
    return value or None


def collect_unit_context(unit: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "result": _unit_property(unit, "Result"),
            "exec_main_status": _unit_property(unit, "ExecMainStatus"),
            "exec_main_code": _unit_property(unit, "ExecMainCode"),
            "n_restarts": _unit_property(unit, "NRestarts"),
            "invocation_id": _unit_property(unit, "InvocationID"),
            "active_state": _unit_property(unit, "ActiveState"),
        }.items()
        if value is not None
    }


def normalize_unit(raw: str) -> str:
    unit = str(raw or "").strip()
    if unit and "." not in unit:
        unit = f"{unit}.service"
    return unit


def report_unit_failure(unit: str, *, context: dict[str, Any] | None = None,
                        conn=None) -> dict[str, Any]:
    """Record one failed unit. Never raises."""
    unit = normalize_unit(unit)
    if not unit:
        return {"reported": False, "reason": "missing_unit"}
    if unit in NEVER_REPORT or is_self_alerting(unit):
        _stderr(
            "systemd_unit_failure_report_refused",
            unit=unit,
            reason="unit_is_part_of_the_alert_path",
        )
        return {"reported": False, "unit": unit, "reason": "self_alerting_unit_refused"}

    detail = dict(context or collect_unit_context(unit))
    result = report_operational_failure(
        incident_code=INCIDENT_UNIT_FAILURE,
        title=f"systemd unit failed: {unit}",
        summary=(
            f"{unit} entered a failed state. This path fires from PID 1, so it also "
            f"covers executions that died before they could register a platform run — "
            f"the class of failure that otherwise exists only in journald."
        ),
        component=f"{COMPONENT_PREFIX}.{unit}",
        severity="error",
        subject_type="systemd_unit",
        subject_key=unit,
        error_message=f"unit={unit} result={detail.get('result')} status={detail.get('exec_main_status')}",
        suggested_action=(
            f"Inspect `systemctl status {unit}` and "
            f"`journalctl -u {unit} -n 200 --no-pager`, then resolve the cause and "
            f"re-run the missed execution."
        ),
        details=detail,
        extra_identity={"unit": unit, "result": detail.get("result")},
        occurred_at=utcnow(),
        conn=conn,
    )
    return {
        "reported": True,
        "unit": unit,
        "incident_id": getattr(result, "incident_id", None),
        "email_enqueued": bool(getattr(result, "email_enqueued", False)),
        "suppression_reason": getattr(result, "suppression_reason", None),
        "error": getattr(result, "error", None),
    }


def _stderr(event: str, **fields: Any) -> None:
    payload = {
        "level": "ERROR",
        "classification": "operational_error",
        "component": "ops.systemd_failure_adapter",
        "event": event,
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    try:
        print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="systemd OnFailure= incident adapter")
    parser.add_argument(
        "unit", nargs="?", default=os.getenv("MONITORED_UNIT"),
        help="Failed unit name; systemd supplies this as the %%i instance specifier",
    )
    args = parser.parse_args(argv)

    outcome = report_unit_failure(args.unit or "")
    print(json.dumps(outcome, sort_keys=True, default=str), flush=True)
    # Always exit 0: a non-zero OnFailure handler is itself a failed unit, and a
    # failure handler that can fail is a loop waiting to happen.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
