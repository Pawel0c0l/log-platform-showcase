#!/usr/bin/env python3
"""The only way a Telematics `trips_sync` run may pass a disabled-schedule guard.

WHAT THIS PROTECTS.
    `jobs.api.telematics.sync_trips_and_speeding` refuses to do any work when the
    dataset schedule is disabled. That refusal is correct and stays the default:
    an ordinary invocation against a disabled schedule performs zero provider
    requests and returns.

    A reviewed cold-start recovery legitimately needs to run *while* the
    schedule is still disabled, because the onboarding order activates the
    schedule only after a verified recovery. Historically that need was met by
    letting the recovery launcher treat the guard's early return as a success —
    which is precisely the defect this module exists to make impossible.

WHAT AN AUTHORITY IS.
    Not a boolean. Not a command-line flag. Not a log line. An authority is the
    conjunction of three independent things, all of which must agree:

      1. **job parameters** — trigger `MANUAL_RECOVERY`, the exact client,
         schedule, dataset, recovery-run UUID, window and the explicit
         disabled-schedule flag;
      2. **a launch attestation** carried out-of-band in the environment,
         naming the same identities. It moves through a channel the job
         parameters do not use, so a parameter set alone — however complete —
         never reaches this path;
      3. **the durable recovery row** in
         `workflow_a_control.client_dataset_recovery_run`, which must exist, be
         `RUNNING`, belong to the same client, schedule and dataset, and carry
         byte-identical window bounds.

    Any disagreement is a hard refusal that fails the run. A bare boolean, a
    direct job invocation, a stale recovery UUID, a wrong client, a wrong
    schedule or a wrong window each fail on their own.

WHAT THIS IS AND IS NOT — THE ACTUAL TRUST LEVEL.
    This is an **operational attestation and accidental-misuse guard**, not a
    security boundary and not an authentication mechanism.

    What it genuinely provides:

      * **binding to durable state** — the substantive authorization gate is the
        `RUNNING` recovery row in the control plane, not the attestation. Every
        identity and window bound in the attestation and the job parameters is
        re-checked against that row. The row is claimed by
        `ops/recover_telematics_trips_window.py` in the *same* invocation that
        launches this job: one transaction inserts exactly one `RUNNING` row
        after re-evaluating every gate under the claim locks, and only then is
        the attestation built and the child launched. What the operator supplies
        beforehand is the reviewed **approval** — client, dataset, window,
        expected watermark, reason and approval reference — not a hand-written
        row;
      * **dispatcher isolation** — the dispatcher neither creates nor inherits
        an attestation (see below), so a scheduled fire can never reach the
        disabled-schedule path;
      * **defeat of accidental invocation** — no ordinary mistake (a re-run of a
        stale command, a copied parameter set, a hand-typed runner invocation, a
        bare flag) satisfies the complete conjunction.

    What it explicitly does **not** provide:

      * it is **not** proof of launcher provenance. `build_launch_attestation`
        contains no secret, no random capability and no launcher-only value:
        every field is a module constant or an identifier the operator already
        supplies. A local process running as the same operating system user that
        knows the client, schedule, dataset, recovery-run UUID and window can
        construct a byte-equivalent attestation. The check that the attestation
        names this launcher is a consistency check, not authentication;
      * it is therefore **not** a secret, a capability token or a cryptographic
        proof, and offers no defense against a malicious same-user process, an
        operator deliberately constructing the attestation, a compromised
        service account, or compromised platform database credentials.

    That trust level is deliberate and accepted: the threat this module is built
    against is operational misuse, and the durable recovery row is what makes a
    forged attestation useless on its own.

WHY THE DISPATCHER CAN NEVER HOLD ONE.
    Three independent reasons, none of which relies on the other two:

      * the dispatcher only ever loads `enabled = true` schedules, so it never
        reaches a disabled-schedule guard at all;
      * `dispatcher._build_job_params` refuses, by construction, to emit any
        parameter of this authority;
      * `dispatcher._launch_job` strips `TELEMATICS_MANUAL_RECOVERY_AUTHORITY`
        from the environment it hands to every subprocess, so an authority
        present in the dispatcher's own environment cannot be inherited.

WHAT IT NEVER DOES.
    It writes nothing. It creates, edits and reclassifies no
    `client_schedule_run_history` row, synthesizes no scheduled fire, and never
    enables a schedule. It reads three rows and returns evidence.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

MANUAL_RECOVERY_TRIGGER = "MANUAL_RECOVERY"

#: Environment variable carrying the launch attestation. Set by
#: `ops/recover_telematics_trips_window.py` for exactly one subprocess, and
#: explicitly stripped by the dispatcher. Not a secret: it is an out-of-band
#: channel, not an authenticated one.
MANUAL_RECOVERY_AUTHORITY_ENV = "TELEMATICS_MANUAL_RECOVERY_AUTHORITY"

AUTHORITY_VERSION = "telematics-manual-recovery-authority/1"

#: The launcher name an accepted attestation must carry. A consistency check
#: against the reviewed recovery path, not a proof that the process was in fact
#: that launcher — nothing here authenticates the caller.
AUTHORIZED_LAUNCHER = "ops/recover_telematics_trips_window.py"

#: The explicit per-invocation opt-in, required in the job parameters.
PARAM_DISABLED_SCHEDULE_FLAG = "allow_disabled_schedule_manual_recovery"
PARAM_RECOVERY_RUN_ID = "manual_recovery_run_id"
PARAM_EXPECTED_SCHEDULE_ID = "expected_schedule_id"

#: The parameters that exist *only* for a disabled-schedule execution. An
#: ordinary enabled-schedule recovery emits neither.
DISABLED_SCHEDULE_AUTHORITY_PARAM_KEYS = frozenset({
    PARAM_DISABLED_SCHEDULE_FLAG,
    PARAM_EXPECTED_SCHEDULE_ID,
})

#: Every job parameter that participates in this authority, including
#: `manual_recovery_run_id`, which an enabled-schedule recovery legitimately
#: carries but the dispatcher must never emit. The dispatcher is forbidden from
#: emitting any of them.
AUTHORITY_PARAM_KEYS = DISABLED_SCHEDULE_AUTHORITY_PARAM_KEYS | frozenset({
    PARAM_RECOVERY_RUN_ID,
})

#: The recovery row state a running business execution must be claimed under.
EXPECTED_RECOVERY_STATUS = "RUNNING"

TRIPS_SYNC_DATASET_NAME = "trips_sync"


class ManualRecoveryAuthorityError(RuntimeError):
    """A refused authority. Bounded, sanitized, never carrying a payload."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _refuse(code: str, message: str) -> ManualRecoveryAuthorityError:
    return ManualRecoveryAuthorityError(code, message)


def _canonical_uuid(value: object, *, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise _refuse(
            "AUTHORITY_REFUSED_IDENTITY", f"{label} is not a canonical UUID"
        ) from exc


def _instant(value: object, *, label: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise _refuse("AUTHORITY_REFUSED_WINDOW", f"{label} is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise _refuse(
            "AUTHORITY_REFUSED_WINDOW", f"{label} is not an ISO-8601 instant"
        ) from exc
    if parsed.utcoffset() is None:
        raise _refuse(
            "AUTHORITY_REFUSED_WINDOW", f"{label} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# The launch attestation
# ---------------------------------------------------------------------------

def build_launch_attestation(
    *,
    client_id: str,
    client_code: Optional[str],
    schedule_id: str,
    dataset_name: str,
    recovery_run_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
) -> str:
    """Serialize the launch attestation for one subprocess.

    Carries no secret, no random value and no capability of its own: every field
    is either a module constant or an identifier the caller already holds, so a
    same-user process with those identifiers can produce the identical string.
    That is accepted, because on its own the attestation authorizes nothing —
    every identity in it is re-checked against the job parameters *and* against
    the durable `RUNNING` recovery row, which is the substantive gate.
    """
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return json.dumps(
        {
            "version": AUTHORITY_VERSION,
            "launcher": AUTHORIZED_LAUNCHER,
            "client_id": str(client_id),
            "client_code": None if client_code is None else str(client_code),
            "schedule_id": str(schedule_id),
            "dataset_name": str(dataset_name),
            "recovery_run_id": str(recovery_run_id),
            "window_start_ts": _iso(window_start_ts),
            "window_end_ts": _iso(window_end_ts),
        },
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def _read_attestation(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    source = os.environ if env is None else env
    raw = str(source.get(MANUAL_RECOVERY_AUTHORITY_ENV) or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _refuse(
            "AUTHORITY_REFUSED_ATTESTATION",
            "the launch attestation is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise _refuse(
            "AUTHORITY_REFUSED_ATTESTATION",
            "the launch attestation is not a JSON object",
        )
    if str(payload.get("version")) != AUTHORITY_VERSION:
        raise _refuse(
            "AUTHORITY_REFUSED_ATTESTATION",
            f"the launch attestation version is not {AUTHORITY_VERSION!r}",
        )
    if str(payload.get("launcher")) != AUTHORIZED_LAUNCHER:
        raise _refuse(
            "AUTHORITY_REFUSED_ATTESTATION",
            "the launch attestation does not name the reviewed recovery path",
        )
    return payload


def authority_requested(
    params: Mapping[str, Any], *, env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Did *anything* about this invocation claim manual-recovery authority?

    Used to separate two very different situations at the disabled-schedule
    guard: an ordinary invocation that must simply skip, and an invocation that
    asked for the authority and must therefore be validated to the letter or
    fail the run. An invocation that half-asks — a bare flag, a stale UUID, an
    attestation without parameters — lands here and is then refused, never
    silently downgraded to a skip.
    """
    source = os.environ if env is None else env
    if str(source.get(MANUAL_RECOVERY_AUTHORITY_ENV) or "").strip():
        return True
    if str(params.get("trigger") or "") == MANUAL_RECOVERY_TRIGGER:
        return True
    return any(str(params.get(key) or "").strip() for key in AUTHORITY_PARAM_KEYS)


# ---------------------------------------------------------------------------
# The full check
# ---------------------------------------------------------------------------

def authorize_disabled_schedule_recovery(
    *,
    params: Mapping[str, Any],
    schedule,
    client_id: str,
    dataset_name: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
    recovery_row_loader,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Validate every condition, or raise. Returns bounded evidence.

    `recovery_row_loader(recovery_run_id)` must return the durable recovery row
    as a mapping (or None). It is injected rather than imported so this module
    holds no connection of its own and stays unit-testable without a database.

    Ordering is deliberate: cheap, purely local disagreements are refused before
    any database read, so a malformed invocation never touches the control
    plane.
    """
    # --- 1) the invocation must be a manual recovery, explicitly -----------
    trigger = str(params.get("trigger") or "")
    if trigger != MANUAL_RECOVERY_TRIGGER:
        raise _refuse(
            "AUTHORITY_REFUSED_TRIGGER",
            f"trigger is {trigger!r}; a disabled-schedule execution requires "
            f"{MANUAL_RECOVERY_TRIGGER!r}",
        )
    flag = params.get(PARAM_DISABLED_SCHEDULE_FLAG)
    if flag is not True:
        raise _refuse(
            "AUTHORITY_REFUSED_FLAG",
            f"{PARAM_DISABLED_SCHEDULE_FLAG} must be the JSON boolean true; a "
            "disabled-schedule execution is never implicit",
        )
    if dataset_name != TRIPS_SYNC_DATASET_NAME:
        raise _refuse(
            "AUTHORITY_REFUSED_DATASET",
            f"this authority is {TRIPS_SYNC_DATASET_NAME}-only",
        )
    recovery_run_id = _canonical_uuid(
        params.get(PARAM_RECOVERY_RUN_ID), label=PARAM_RECOVERY_RUN_ID
    )
    param_schedule_id = _canonical_uuid(
        params.get(PARAM_EXPECTED_SCHEDULE_ID), label=PARAM_EXPECTED_SCHEDULE_ID
    )
    client_id = str(client_id)

    # --- 2) the launch attestation ----------------------------------------
    attestation = _read_attestation(env)
    if attestation is None:
        raise _refuse(
            "AUTHORITY_REFUSED_ATTESTATION",
            "no launch attestation is present; a disabled-schedule execution "
            "requires the out-of-band attestation the reviewed recovery path "
            "sets, and job parameters alone are never sufficient",
        )
    expected = {
        "client_id": client_id,
        "schedule_id": param_schedule_id,
        "dataset_name": dataset_name,
        "recovery_run_id": recovery_run_id,
    }
    for field, value in expected.items():
        if str(attestation.get(field) or "") != value:
            raise _refuse(
                "AUTHORITY_REFUSED_ATTESTATION",
                f"the launch attestation {field} does not match the job "
                "parameters",
            )
    if _instant(
        attestation.get("window_start_ts"), label="attested window_start_ts"
    ) != window_start_ts:
        raise _refuse(
            "AUTHORITY_REFUSED_WINDOW",
            "the launch attestation window start does not match the job window",
        )
    if _instant(
        attestation.get("window_end_ts"), label="attested window_end_ts"
    ) != window_end_ts:
        raise _refuse(
            "AUTHORITY_REFUSED_WINDOW",
            "the launch attestation window end does not match the job window",
        )

    # --- 3) the schedule this run actually loaded --------------------------
    if not getattr(schedule, "exists", False):
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "no client_dataset_schedule row exists; a disabled-schedule "
            "recovery is authorized against an existing authoritative schedule",
        )
    if bool(getattr(schedule, "enabled", False)):
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "the schedule is enabled; this authority exists only for a "
            "schedule that has never been allowed to fire",
        )
    schedule_id = getattr(schedule, "schedule_id", None)
    if not schedule_id:
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "the loaded schedule carries no schedule_id",
        )
    schedule_id = _canonical_uuid(schedule_id, label="schedule_id")
    if schedule_id != param_schedule_id:
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "the loaded schedule is not the schedule this recovery is "
            "authorized against",
        )
    if str(getattr(schedule, "dataset_name", "")) != dataset_name:
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "the loaded schedule is for a different dataset",
        )

    # --- 4) the durable recovery row --------------------------------------
    row = recovery_row_loader(recovery_run_id)
    if not row:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY",
            "no recovery row exists for the declared recovery-run id; a bare "
            "flag never authorizes a disabled-schedule execution",
        )
    if str(row.get("status") or "") != EXPECTED_RECOVERY_STATUS:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_STATE",
            f"the recovery row is {row.get('status')!r}, expected "
            f"{EXPECTED_RECOVERY_STATUS!r}; a terminal or unclaimed recovery "
            "authorizes nothing",
        )
    if str(row.get("client_id") or "") != client_id:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_IDENTITY",
            "the recovery row belongs to another client",
        )
    if str(row.get("schedule_id") or "") != schedule_id:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_IDENTITY",
            "the recovery row belongs to another schedule",
        )
    if str(row.get("dataset_name") or "") != dataset_name:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_IDENTITY",
            "the recovery row belongs to another dataset",
        )
    row_start = row.get("window_start_ts")
    row_end = row.get("window_end_ts")
    if not isinstance(row_start, datetime) or not isinstance(row_end, datetime):
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_WINDOW",
            "the recovery row carries no usable window bounds",
        )
    if row_start.astimezone(timezone.utc) != window_start_ts or \
            row_end.astimezone(timezone.utc) != window_end_ts:
        raise _refuse(
            "AUTHORITY_REFUSED_RECOVERY_WINDOW",
            "the recovery row window does not match the job window; a run "
            "never executes an interval its recovery was not approved for",
        )
    # The recovery row also records whether the schedule was disabled when the
    # recovery was claimed. Re-reading `enabled` here would race; the loader is
    # asked instead to report the schedule state it observed in the same
    # transaction as the recovery row.
    if row.get("schedule_enabled") is True:
        raise _refuse(
            "AUTHORITY_REFUSED_SCHEDULE",
            "the schedule was enabled between the claim and this execution; a "
            "disabled-schedule recovery runs only while it stays disabled",
        )

    return {
        "authority_version": AUTHORITY_VERSION,
        "launcher": AUTHORIZED_LAUNCHER,
        "trigger": MANUAL_RECOVERY_TRIGGER,
        "client_id": client_id,
        "schedule_id": schedule_id,
        "dataset_name": dataset_name,
        "recovery_run_id": recovery_run_id,
        "recovery_status": EXPECTED_RECOVERY_STATUS,
        "schedule_enabled": False,
        "schedule_history_rows_synthesized": 0,
        "schedule_rows_modified": 0,
    }


def strip_authority_from_env(env: Dict[str, str]) -> Dict[str, str]:
    """Remove the attestation from an environment about to be inherited.

    Used by every launcher that is *not* the reviewed recovery path, so an
    authority present in a parent process can never leak into a child.
    """
    env.pop(MANUAL_RECOVERY_AUTHORITY_ENV, None)
    return env


def reject_authority_params(params: Mapping[str, Any], *, surface: str) -> None:
    """Deny-by-default guard for any surface forbidden to hold an authority."""
    present = sorted(key for key in AUTHORITY_PARAM_KEYS if key in params)
    if str(params.get("trigger") or "") == MANUAL_RECOVERY_TRIGGER:
        present.append("trigger")
    if present:
        raise ValueError(
            f"{surface} must never emit manual-recovery authority parameters: "
            f"{', '.join(sorted(set(present)))}"
        )
