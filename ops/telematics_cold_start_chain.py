#!/usr/bin/env python3
"""Pure helpers for a multi-window Telematics cold-start recovery chain.

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §13.6a (the cold-start state
    machine, extended here from one recovery to a chain of one or more)
  docs/07_operations.md §5.5 (the operator procedure)
  db/migrations/058_telematics_trips_manual_recovery.sql (the recovery evidence
    table this module reads through its callers; it is never written here)

WHY THIS MODULE EXISTS.
    A cold start whose approved range is longer than the client's
    ``trips_max_recovery_span_seconds`` cannot be closed by one recovery. It
    needs an ordered chain::

        baseline W -> W1 -> W2 -> ... -> final approved W -> activation

    Two tools must agree, exactly, on what that chain is: the recovery tool has
    to decide whether a *next* window may proceed, and the activation tool has
    to decide whether the chain is *complete*. Any disagreement between those two
    judgements is a hole, so the judgement lives here once, as pure functions
    over plain values.

WHAT IS PURE HERE.
    Everything. This module opens no connection, issues no SQL, runs no
    subprocess, reads no environment and imports nothing from the repository. It
    receives already-fetched rows as mappings and returns verdicts. That is what
    makes it testable without a database and what keeps the two callers honest.

CHAIN IDENTITY WITHOUT A MIGRATION.
    Migration 058 has no chain column, and inventing one for convenience is
    explicitly out of scope. It does have ``approval_ref``: ``NOT NULL``, bounded,
    already part of the reviewed recovery contract, and already carried by the
    uniqueness index ``uq_client_dataset_recovery_run_approved_window``. A chain
    is therefore expressed as a *structural composition* of that existing field::

        chain ref            TELEMATICS-COLD-START-ECHO00001-2026-08
        window approval ref  TELEMATICS-COLD-START-ECHO00001-2026-08-W01
                             TELEMATICS-COLD-START-ECHO00001-2026-08-W02

    This is unambiguous rather than merely conventional, because a chain
    reference may not itself end in a ``-W<digits>`` segment (see
    ``validate_chain_ref``). With that single restriction the trailing window
    segment of an approval reference is decidable without context: strip it, and
    what remains is the one chain reference that could have produced it. Two
    different chains can never produce the same window approval reference, and no
    window approval reference can be mistaken for a chain reference.

    Each window still carries its own unique approval reference, so the database
    uniqueness index keeps its full meaning: re-running an approved window is
    still refused by PostgreSQL, not only by tooling discipline.

    ``reason`` is deliberately *not* used to carry chain identity. It is a free
    human sentence with no structural contract, and binding execution
    authorization to prose would be a weaker claim than the one made here.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

CHAIN_SEMANTICS_VERSION = "telematics-cold-start-chain/1"

# A chain reference is a bounded safe token drawn from the same alphabet the
# recovery tool already accepts for `--approval-ref`, minus anything that would
# make the composed approval reference ambiguous or over-long.
#
# 180 characters leaves room for the longest window segment this module will
# ever emit while staying inside migration 058's 200-character `approval_ref`
# bound, so a valid chain reference can never produce an approval reference the
# database would reject.
MAX_CHAIN_REF_CHARS = 180
MAX_APPROVAL_REF_CHARS = 200

CHAIN_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,179}$")

# The window segment: `-W` followed by at least two digits. Two digits is a
# floor, not a cap: `-W100` is accepted and orders after `-W99`, because the
# ordinal is compared as an integer and never as text.
WINDOW_SEGMENT_RE = re.compile(r"^(?P<chain>.+)-W(?P<ordinal>[0-9]{2,})$")

MAX_WINDOW_ORDINAL = 999


class ChainContractError(ValueError):
    """A pure, caller-classified violation of the chain contract.

    Carries a stable ``code`` so both callers can map it to their own refusal
    vocabulary without string-matching a message.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Chain identity
# ---------------------------------------------------------------------------

def validate_chain_ref(value: object) -> str:
    """Return the canonical chain reference, or raise ``ChainContractError``.

    The one restriction beyond the safe-token alphabet is that a chain reference
    may not itself end in a window segment. Without it, the chain
    ``X`` with window ``X-W01`` and the chain ``X-W01`` with window ``X-W01-W01``
    would still be distinguishable, but the chain ``X-W0`` would not be
    distinguishable from a malformed member of chain ``X``; forbidding the shape
    outright is cheaper to reason about than enumerating which collisions are
    survivable.
    """
    text = str(value or "").strip()
    if not CHAIN_REF_RE.match(text):
        raise ChainContractError(
            "CHAIN_REF_INVALID",
            "the chain reference must be 1-"
            f"{MAX_CHAIN_REF_CHARS} characters of [A-Za-z0-9._:@/+-] and start "
            "with an alphanumeric",
        )
    if WINDOW_SEGMENT_RE.match(text):
        raise ChainContractError(
            "CHAIN_REF_INVALID",
            "the chain reference must not itself end in a -W<NN> window "
            "segment; that suffix is reserved so a window approval reference "
            "can never be mistaken for a chain reference",
        )
    return text


def window_approval_ref(chain_ref: str, ordinal: int) -> str:
    """Compose the approval reference for one window of a chain."""
    chain = validate_chain_ref(chain_ref)
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise ChainContractError(
            "CHAIN_ORDINAL_INVALID", "the window ordinal must be an integer",
        )
    if ordinal < 1 or ordinal > MAX_WINDOW_ORDINAL:
        raise ChainContractError(
            "CHAIN_ORDINAL_INVALID",
            f"the window ordinal must be between 1 and {MAX_WINDOW_ORDINAL}",
        )
    composed = f"{chain}-W{ordinal:02d}"
    if len(composed) > MAX_APPROVAL_REF_CHARS:
        raise ChainContractError(
            "CHAIN_REF_INVALID",
            "the composed approval reference exceeds the "
            f"{MAX_APPROVAL_REF_CHARS}-character contract bound",
        )
    return composed


def parse_window_approval_ref(value: object) -> Optional[Tuple[str, int]]:
    """Split an approval reference into ``(chain_ref, ordinal)``, or ``None``.

    ``None`` means "this approval reference is not a chain window" — which is
    exactly what an unrelated recovery row looks like, so the callers treat a
    ``None`` here as foreign state rather than as an error.
    """
    text = str(value or "").strip()
    match = WINDOW_SEGMENT_RE.match(text)
    if match is None:
        return None
    chain = match.group("chain")
    try:
        validate_chain_ref(chain)
    except ChainContractError:
        return None
    ordinal = int(match.group("ordinal"))
    if ordinal < 1 or ordinal > MAX_WINDOW_ORDINAL:
        return None
    # Re-composition must round-trip exactly. `-W007` parses to 7 but is not the
    # canonical rendering of window 7, and accepting both spellings would let one
    # window be approved twice under two different references.
    if window_approval_ref(chain, ordinal) != text:
        return None
    return chain, ordinal


def belongs_to_chain(approval_ref: object, chain_ref: str) -> bool:
    parsed = parse_window_approval_ref(approval_ref)
    return parsed is not None and parsed[0] == chain_ref


# ---------------------------------------------------------------------------
# Deterministic window planning
# ---------------------------------------------------------------------------

def _require_instant(value: object, *, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ChainContractError(
            "PLAN_BOUND_INVALID", f"{label} must be a datetime",
        )
    if value.utcoffset() is None:
        raise ChainContractError(
            "PLAN_BOUND_INVALID", f"{label} must be timezone-aware",
        )
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond != 0:
        raise ChainContractError(
            "PLAN_BOUND_INVALID",
            f"{label} must be a whole-second instant; it is never rounded here",
        )
    return normalized


def plan_recovery_windows(
    *,
    start: datetime,
    final_end: datetime,
    max_span_seconds: int,
) -> List[Tuple[datetime, datetime]]:
    """Split ``[start, final_end]`` into consecutive windows of at most ``R``.

    The split is total and deterministic:

    * the first window starts exactly at ``start``;
    * every later window starts exactly where the previous one ended — no
      one-second gap is introduced, because the recovery contract anchors a
      window at ``W`` itself and never at ``W + 1 s``;
    * no two windows overlap;
    * the last window ends exactly at ``final_end``, never past it;
    * every window spans at least one second and at most ``max_span_seconds``.

    Whole-second handling matches the recovery contract: bounds must already be
    whole-second instants and nothing here rounds, floors or pads them. A range
    that is not an exact multiple of ``R`` therefore ends with one short window
    rather than with a widened final boundary.

    This is planning only. It authorizes nothing: the operator still approves and
    executes each window separately, and the recovery tool re-derives and
    re-checks every bound against live state.
    """
    first = _require_instant(start, label="start")
    last = _require_instant(final_end, label="final_end")
    if not isinstance(max_span_seconds, int) or isinstance(max_span_seconds, bool):
        raise ChainContractError(
            "PLAN_SPAN_INVALID", "max_span_seconds must be an integer",
        )
    if max_span_seconds <= 0:
        raise ChainContractError(
            "PLAN_SPAN_INVALID", "max_span_seconds must be strictly positive",
        )
    if last <= first:
        raise ChainContractError(
            "PLAN_BOUND_INVALID",
            "final_end must be strictly after start; a range that cannot "
            "advance W is an operator error, not an empty plan",
        )
    total = int((last - first).total_seconds())
    if total > MAX_WINDOW_ORDINAL * max_span_seconds:
        raise ChainContractError(
            "PLAN_TOO_MANY_WINDOWS",
            f"the range needs more than {MAX_WINDOW_ORDINAL} windows at "
            f"{max_span_seconds}s each",
        )

    windows: List[Tuple[datetime, datetime]] = []
    cursor = first
    step = timedelta(seconds=max_span_seconds)
    while cursor < last:
        end = min(cursor + step, last)
        windows.append((cursor, end))
        cursor = end
    return windows


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_window_plan(
    windows: Sequence[Tuple[datetime, datetime]],
    *,
    chain_ref: Optional[str] = None,
    start_ordinal: int = 1,
) -> List[Dict[str, Any]]:
    """Stable, reviewable rendering of a plan.

    ``start_ordinal`` lets a caller render the *remaining* windows of a chain
    with their true positions, so a review of window 2 shows ``W02`` rather than
    a misleading ``W01``.
    """
    rendered: List[Dict[str, Any]] = []
    for index, (start, end) in enumerate(windows, start=start_ordinal):
        item: Dict[str, Any] = {
            "window_ordinal": index,
            "window_start_ts": _iso(start),
            "window_end_ts": _iso(end),
            "window_span_seconds": int((end - start).total_seconds()),
        }
        if chain_ref is not None:
            item["approval_ref"] = window_approval_ref(chain_ref, index)
        rendered.append(item)
    return rendered


# ---------------------------------------------------------------------------
# Chain state evaluation
# ---------------------------------------------------------------------------

TERMINAL_SUCCESS = "SUCCESS"
ACTIVE_STATUSES = frozenset({"PLANNED", "RUNNING"})


def _utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def partition_recovery_rows(
    rows: Sequence[Mapping[str, Any]], *, chain_ref: str,
) -> Dict[str, List[Mapping[str, Any]]]:
    """Split every recovery row attributable to the target into two buckets.

    ``chain`` holds the rows whose ``approval_ref`` is a well-formed window of
    exactly this chain. ``foreign`` holds everything else: another chain, a
    historical C11 recovery, a hand-written approval reference, or a malformed
    one. Foreign rows are never silently tolerated by either caller — an
    unrelated recovery is precisely the pre-existing execution state a cold start
    must refuse.
    """
    chain: List[Mapping[str, Any]] = []
    foreign: List[Mapping[str, Any]] = []
    for row in rows:
        if belongs_to_chain(row.get("approval_ref"), chain_ref):
            chain.append(row)
        else:
            foreign.append(row)
    return {"chain": chain, "foreign": foreign}


def chain_window_ordinal(row: Mapping[str, Any]) -> int:
    parsed = parse_window_approval_ref(row.get("approval_ref"))
    if parsed is None:  # pragma: no cover - callers partition first
        raise ChainContractError(
            "CHAIN_MEMBERSHIP_INVALID",
            "the row is not a chain window",
        )
    return parsed[1]


def evaluate_chain(
    chain_rows: Sequence[Mapping[str, Any]],
    *,
    baseline_ts: datetime,
    current_covered_through_ts: datetime,
    client_id: str,
    schedule_id: str,
    dataset_name: str,
) -> Dict[str, Any]:
    """Prove that the chain rows form one complete, contiguous SUCCESS prefix.

    Returns a summary on success. Raises ``ChainContractError`` on the first
    violation, in a fixed order so the refusal an operator sees is stable:

    1. identity — every row belongs to this exact client, schedule and dataset;
    2. liveness — no row is still ``PLANNED`` or ``RUNNING``;
    3. outcome — every row is ``SUCCESS``; one ``FAILED`` or
       ``FINALIZATION_CONFLICT`` row blocks the whole chain and is never skipped
       merely because a later window was requested;
    4. ordinals — exactly ``1..N``, no gap, no duplicate;
    5. geometry — the first window starts at the original baseline, each later
       window starts exactly where the previous one ended, every window is
       forward, and the last window ends exactly at the current watermark.

    An empty ``chain_rows`` is a valid input and describes the pre-chain state;
    the caller decides whether that means "first window" or "nothing to
    activate".
    """
    baseline = _require_instant(baseline_ts, label="baseline_ts")
    current_w = _require_instant(
        current_covered_through_ts, label="current_covered_through_ts",
    )
    if not chain_rows:
        return {
            "chain_semantics_version": CHAIN_SEMANTICS_VERSION,
            "successful_window_count": 0,
            "windows": [],
            "next_window_ordinal": 1,
            "next_window_start_ts": _iso(baseline),
        }

    for row in chain_rows:
        if str(row.get("client_id")) != str(client_id) or \
                str(row.get("schedule_id")) != str(schedule_id) or \
                str(row.get("dataset_name")) != str(dataset_name):
            raise ChainContractError(
                "CHAIN_IDENTITY_MISMATCH",
                "a chain recovery row does not belong to this exact client, "
                "schedule and dataset",
            )

    active = [
        row for row in chain_rows
        if str(row.get("status")) in ACTIVE_STATUSES
    ]
    if active:
        raise ChainContractError(
            "CHAIN_WINDOW_ACTIVE",
            f"{len(active)} chain recovery row(s) are still PLANNED or RUNNING; "
            "a chain never continues across an unfinished window",
        )

    unsuccessful = [
        row for row in chain_rows
        if str(row.get("status")) != TERMINAL_SUCCESS
    ]
    if unsuccessful:
        raise ChainContractError(
            "CHAIN_WINDOW_NOT_SUCCESS",
            f"{len(unsuccessful)} chain recovery row(s) are not SUCCESS; a "
            "failed window is preserved and blocks the chain until an operator "
            "decides explicitly",
        )

    ordered = sorted(chain_rows, key=chain_window_ordinal)
    ordinals = [chain_window_ordinal(row) for row in ordered]
    if ordinals != list(range(1, len(ordered) + 1)):
        raise ChainContractError(
            "CHAIN_ORDINAL_SEQUENCE_INVALID",
            "the chain window ordinals are not exactly 1..N without gaps or "
            "duplicates",
        )

    windows: List[Dict[str, Any]] = []
    previous_end: Optional[datetime] = None
    for ordinal, row in zip(ordinals, ordered):
        start = _utc(row.get("window_start_ts"))
        end = _utc(row.get("window_end_ts"))
        if start is None or end is None:
            raise ChainContractError(
                "CHAIN_GEOMETRY_INVALID",
                "a chain recovery row has no interval",
            )
        if end <= start:
            raise ChainContractError(
                "CHAIN_GEOMETRY_INVALID",
                "a chain recovery window does not advance",
            )
        expected_start = baseline if previous_end is None else previous_end
        if start != expected_start:
            raise ChainContractError(
                "CHAIN_GEOMETRY_INVALID",
                "the chain is not contiguous: window "
                f"{ordinal} does not start where the previous one ended "
                "(window 1 must start at the original cold-start baseline)",
            )
        windows.append({
            "window_ordinal": ordinal,
            "recovery_run_id": (
                None if row.get("recovery_run_id") is None
                else str(row.get("recovery_run_id"))
            ),
            "approval_ref": str(row.get("approval_ref")),
            "window_start_ts": _iso(start),
            "window_end_ts": _iso(end),
            "window_span_seconds": int((end - start).total_seconds()),
            "platform_run_id": (
                None if row.get("platform_run_id") is None
                else str(row.get("platform_run_id"))
            ),
        })
        previous_end = end

    if previous_end != current_w:
        raise ChainContractError(
            "CHAIN_GEOMETRY_INVALID",
            "the last successful chain window does not end at the current "
            "coverage watermark; the coverage claim and the executed work "
            "disagree",
        )

    return {
        "chain_semantics_version": CHAIN_SEMANTICS_VERSION,
        "successful_window_count": len(windows),
        "windows": windows,
        "next_window_ordinal": len(windows) + 1,
        "next_window_start_ts": _iso(previous_end),
    }


def evaluate_business_run_correspondence(
    chain_summary: Mapping[str, Any],
    *,
    target_runs: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Prove a one-to-one SUCCESS mapping between chain windows and platform runs.

    ``target_runs`` is every ``public.runs`` row attributable to the target, as
    ``{"run_id": ..., "status": ...}``. The mapping must be exact in both
    directions:

    * every successful chain window names a platform run that exists and is
      ``SUCCESS`` — a window whose run is missing cannot be shown to have done
      the work it claims;
    * no two windows name the same run — a duplicate association would let one
      execution be counted as two closed windows;
    * no target run is left over — an unrelated business run means the client is
      not the controlled, isolated target the cold-start contract assumes.
    """
    windows = list(chain_summary.get("windows") or [])
    runs_by_id = {str(row.get("run_id")): str(row.get("status")) for row in target_runs}

    seen: Dict[str, int] = {}
    for window in windows:
        run_id = window.get("platform_run_id")
        if not run_id:
            raise ChainContractError(
                "CHAIN_RUN_MISSING",
                f"chain window {window.get('window_ordinal')} records no "
                "platform business run; a SUCCESS window without its run is "
                "unverifiable evidence",
            )
        run_id = str(run_id)
        if run_id in seen:
            raise ChainContractError(
                "CHAIN_RUN_DUPLICATE",
                "two chain windows reference the same platform business run",
            )
        seen[run_id] = int(window.get("window_ordinal") or 0)
        if run_id not in runs_by_id:
            raise ChainContractError(
                "CHAIN_RUN_MISSING",
                f"the platform business run of chain window "
                f"{window.get('window_ordinal')} does not exist for this target",
            )
        if runs_by_id[run_id] != TERMINAL_SUCCESS:
            raise ChainContractError(
                "CHAIN_RUN_NOT_SUCCESS",
                f"the platform business run of chain window "
                f"{window.get('window_ordinal')} is {runs_by_id[run_id]}, "
                "expected SUCCESS",
            )

    unrelated = sorted(set(runs_by_id) - set(seen))
    if unrelated:
        raise ChainContractError(
            "CHAIN_RUN_UNRELATED",
            f"{len(unrelated)} platform business run(s) reference this target "
            "but belong to no chain window",
        )
    return {
        "chain_business_runs": len(seen),
        "target_business_runs": len(runs_by_id),
        "unrelated_business_runs": 0,
    }
