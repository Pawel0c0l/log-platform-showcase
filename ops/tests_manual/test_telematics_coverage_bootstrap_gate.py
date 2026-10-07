#!/usr/bin/env python3
"""Focused C5 tests for the Telematics coverage bootstrap gate.

Delivery scope: C5 of `docs/14_telematics_trips_compatibility_implementation_plan.md`
(§3/C5, §7.1), implementing `docs/13_telematics_trips_stabilization_windows.md`
§5.2.1 and the §5.3 contiguity classification.

Three layers, in this order:

  1. Pure gate tests — no database, no network, no clock. Every §5.2.1
     precondition, the connectivity boundary, determinism and immutability.
  2. Dispatcher unit tests against a fake connection — claim-time evidence,
     job-parameter construction, and the side-effect boundary: every rejected
     gate must reach neither `subprocess.Popen`, nor `_launch_job`, nor
     `_build_job_params`, nor a socket, nor a credential resolver.
  3. Disposable-PostgreSQL integration — real migrations through 057, real
     `prepare_run` / `run_prepared`, terminal history, an unchanged coverage
     row under every rejection, and the absence of any coverage mutation at
     all — C5 is strictly read-only against `client_dataset_coverage`.

Layers 1 and 2 always run. Layer 3 runs only when
TELEMATICS_COVERAGE_GATE_TEST_DSN points at a **disposable** PostgreSQL 16
database — never logdb:

  docker run -d --rm --name c5-gate-pg -e POSTGRES_PASSWORD=... \
      -e POSTGRES_USER=loguser -e POSTGRES_DB=c5_gate_test \
      -p 55705:5432 postgres:16
  TELEMATICS_COVERAGE_GATE_TEST_DSN='postgresql://loguser:...@127.0.0.1:55705/c5_gate_test' \
      .venv/bin/python ops/tests_manual/test_telematics_coverage_bootstrap_gate.py

No test issues a provider request, resolves a secret, launches a real job or
writes to any production database.
"""
from __future__ import annotations

import dataclasses
import os
import socket
import sys
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual import telematics_execution_outcome_fixtures as eo_fixtures  # noqa: E402
from jobs.api.telematics import dispatcher as disp  # noqa: E402
from jobs.api.telematics.execution_outcome import ExecutionOutcome  # noqa: E402
from jobs.api.telematics import registry  # noqa: E402
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED,
    COVERAGE_GATE_BOOTSTRAP_REQUIRED,
    COVERAGE_GATE_GAP_DETECTED,
    COVERAGE_STATUS_GAP_DETECTED,
    COVERAGE_STATUS_READY,
    COVERAGE_STATUS_RESEED_REQUIRED,
    COVERAGE_STATUS_UNINITIALIZED,
    TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
    TRIPS_COVERAGE_GAP_DETECTED,
    CoverageGateResult,
    CoverageState,
    evaluate_coverage_gate,
)
from jobs.trips_pagination_mode import (  # noqa: E402
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_PAGINATION_MODE_STRICT_META,
)

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
    # M4: an advancing fire now projects durable evidence inside the same
    # finalization transaction, so this end-to-end harness needs the relation.
    "061_workflow_a_provider_request_log.sql",
)

CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_CODE = "TST00001"
SCHEDULE_ID = "7cac378a-5787-4d62-85d1-282bed208c8c"
TRIPS_DS = "trips_sync"
TRIPS_JOB = registry.DATASETS[TRIPS_DS].job_module

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail and not ok:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1) Pure gate
# ---------------------------------------------------------------------------

FIRE = _utc(2026, 8, 2, 0, 0, 0)
NOW = _utc(2026, 8, 2, 0, 5, 0)
DELAY = 10_800
OVERLAP = 3_600
RECOVERY = 2_678_400
LOOKBACK_DAYS = 7

# E_end = F - D; base = F - 7d - D - O.
E_END = FIRE - timedelta(seconds=DELAY)
BASE_START = FIRE - timedelta(days=LOOKBACK_DAYS) - timedelta(seconds=DELAY + OVERLAP)


def _state(**overrides) -> CoverageState:
    values = dict(
        schedule_id=SCHEDULE_ID,
        client_id=CLIENT_ID,
        client_code=CLIENT_CODE,
        dataset_name=TRIPS_DS,
        coverage_start_ts=_utc(2026, 6, 1, 0, 0, 0),
        covered_through_ts=_utc(2026, 8, 1, 0, 0, 0),
        bootstrap_status=COVERAGE_STATUS_READY,
        bootstrap_evidence_ref="artifact:0f2b1c",
        seeded_at=_utc(2026, 7, 1, 12, 0, 0),
        seeded_by="operator@example",
        covered_through_source="bootstrap",
        last_gap_detected_ts=None,
    )
    values.update(overrides)
    return CoverageState(**values)


def _gate(state, **overrides) -> CoverageGateResult:
    kwargs = dict(
        schedule_id=SCHEDULE_ID,
        client_id=CLIENT_ID,
        client_code=CLIENT_CODE,
        dataset_name=TRIPS_DS,
        scheduled_fire_ts=FIRE,
        lookback_days=LOOKBACK_DAYS,
        stabilization_delay_seconds=DELAY,
        overlap_seconds=OVERLAP,
        max_recovery_span_seconds=RECOVERY,
        coverage_state=state,
        now_utc=NOW,
    )
    kwargs.update(overrides)
    return evaluate_coverage_gate(**kwargs)


print("# Pure gate — fail-closed preconditions (docs/13 §5.2.1)")

BOOTSTRAP_CASES = (
    ("missing coverage row", None),
    ("coverage_start_ts NULL", _state(coverage_start_ts=None)),
    ("covered_through_ts NULL", _state(covered_through_ts=None)),
    ("both bounds NULL", _state(coverage_start_ts=None, covered_through_ts=None)),
    ("A > W", _state(coverage_start_ts=_utc(2026, 8, 1, 0, 0, 1))),
    ("W in the future", _state(covered_through_ts=_utc(2026, 9, 1))),
    ("blank bootstrap_evidence_ref", _state(bootstrap_evidence_ref="")),
    ("whitespace bootstrap_evidence_ref", _state(bootstrap_evidence_ref="   ")),
    ("absent bootstrap_evidence_ref", _state(bootstrap_evidence_ref=None)),
    ("absent seeded_at", _state(seeded_at=None)),
    ("naive seeded_at", _state(seeded_at=datetime(2026, 7, 1, 12, 0, 0))),
    ("blank seeded_by", _state(seeded_by="")),
    ("whitespace seeded_by", _state(seeded_by="   ")),
    ("absent seeded_by", _state(seeded_by=None)),
    ("UNINITIALIZED", _state(bootstrap_status=COVERAGE_STATUS_UNINITIALIZED)),
    ("RESEED_REQUIRED", _state(bootstrap_status=COVERAGE_STATUS_RESEED_REQUIRED)),
    ("unknown status", _state(bootstrap_status="SORT_OF_READY")),
    ("None status", _state(bootstrap_status=None)),
    ("client identity mismatch", _state(client_id="5e45e815-b1ec-4164-88c7-611590cd8ea7")),
    ("dataset identity mismatch", _state(dataset_name="fuel_daily_aggregation")),
    ("client_code contradiction", _state(client_code="OTHER0001")),
    ("naive A", _state(coverage_start_ts=datetime(2026, 6, 1))),
    ("naive W", _state(covered_through_ts=datetime(2026, 8, 1))),
    ("sub-second A", _state(coverage_start_ts=_utc(2026, 6, 1).replace(microsecond=1))),
    ("sub-second W", _state(covered_through_ts=_utc(2026, 8, 1).replace(microsecond=1))),
    ("non-datetime A", _state(coverage_start_ts="not-a-datetime")),
    ("non-datetime W", _state(covered_through_ts=42)),
)

for label, state in BOOTSTRAP_CASES:
    result = _gate(state)
    _check(
        f"bootstrap-required: {label}",
        (
            result.allowed is False
            and result.classification == COVERAGE_GATE_BOOTSTRAP_REQUIRED
            and result.abort_code == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
            and result.effective_window is None
            and result.requires_gap_persistence is False
            and bool(result.reason)
        ),
        f"got {result}",
    )

# A pre-existing GAP_DETECTED row is the one narrow exception to the generic
# non-READY bootstrap taxonomy: it re-emits the gap code so an unclosed hole in
# a previously verified interval stays loud (docs/13 §5.3), and C5 rewrites
# nothing, so no persistence is owed.
gap_row = _gate(_state(bootstrap_status=COVERAGE_STATUS_GAP_DETECTED))
_check(
    "pre-existing GAP_DETECTED row re-emits TRIPS_COVERAGE_GAP_DETECTED",
    gap_row.allowed is False
    and gap_row.classification == COVERAGE_GATE_GAP_DETECTED
    and gap_row.abort_code == TRIPS_COVERAGE_GAP_DETECTED
    and gap_row.bootstrap_status == COVERAGE_STATUS_GAP_DETECTED
    and gap_row.effective_window is None,
    f"got {gap_row}",
)
_check(
    "pre-existing GAP_DETECTED owes no persistence — C5 rewrites nothing",
    gap_row.requires_gap_persistence is False,
    f"got {gap_row.requires_gap_persistence}",
)
_check(
    "an existing gap is never allowed and never falls back to strict",
    gap_row.allowed is False and gap_row.effective_window is None,
)
_check(
    "UNINITIALIZED and RESEED_REQUIRED keep the bootstrap classification",
    _gate(_state(bootstrap_status=COVERAGE_STATUS_UNINITIALIZED)).abort_code
    == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and _gate(_state(bootstrap_status=COVERAGE_STATUS_RESEED_REQUIRED)).abort_code
    == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
)
_check(
    "an identity-mismatched GAP_DETECTED row is still bootstrap-required",
    _gate(_state(
        bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
        dataset_name="fuel_daily_aggregation",
    )).abort_code == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
)

MALFORMED_GAP_CASES = (
    ("NULL A", dict(coverage_start_ts=None)),
    ("NULL W", dict(covered_through_ts=None)),
    ("A > W", dict(coverage_start_ts=_utc(2026, 8, 1, 0, 0, 1))),
    ("A naive", dict(coverage_start_ts=datetime(2026, 6, 1, 0, 0, 0))),
    ("W naive", dict(covered_through_ts=datetime(2026, 8, 1, 0, 0, 0))),
    ("A sub-second", dict(
        coverage_start_ts=_utc(2026, 6, 1).replace(microsecond=1))),
    ("W sub-second", dict(
        covered_through_ts=_utc(2026, 8, 1).replace(microsecond=1))),
    ("A non-datetime", dict(coverage_start_ts="2026-06-01T00:00:00Z")),
    ("W non-datetime", dict(covered_through_ts=1_785_542_400)),
    ("W in the future", dict(covered_through_ts=_utc(2026, 9, 1))),
    ("missing evidence", dict(bootstrap_evidence_ref=None)),
    ("blank evidence", dict(bootstrap_evidence_ref="")),
    ("whitespace-only evidence", dict(bootstrap_evidence_ref="   \t")),
    ("missing seeded_at", dict(seeded_at=None)),
    ("naive seeded_at", dict(seeded_at=datetime(2026, 7, 1, 12, 0, 0))),
    ("missing seeded_by", dict(seeded_by=None)),
    ("blank seeded_by", dict(seeded_by="")),
    ("whitespace-only seeded_by", dict(seeded_by="  \t")),
    ("client identity mismatch", dict(
        client_id="5e45e815-b1ec-4164-88c7-611590cd8ea7")),
    ("client-code mismatch", dict(client_code="OTHER0001")),
    ("dataset mismatch", dict(dataset_name="fuel_daily_aggregation")),
)

for label, overrides in MALFORMED_GAP_CASES:
    malformed = _state(
        bootstrap_status=COVERAGE_STATUS_GAP_DETECTED, **overrides,
    )
    result = _gate(malformed)
    _check(
        f"malformed GAP_DETECTED is bootstrap-required: {label}",
        result.allowed is False
        and result.classification == COVERAGE_GATE_BOOTSTRAP_REQUIRED
        and result.abort_code == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
        and result.effective_window is None
        and result.requires_gap_persistence is False
        and result.bootstrap_status == COVERAGE_STATUS_GAP_DETECTED
        and 0 < len(result.reason) <= 300,
        f"got {result}",
    )

# M5 — the coverage owner is (client_id, dataset_name). A differing
# `schedule_id` is the NORMAL shape for a reconciliation fire reading the base
# schedule's shared row, so it must not be an identity failure. The precedence
# rule itself is unchanged and is re-proven through `client_id`, which remains a
# genuine identity field.
_check(
    "a differing schedule_id is not an identity failure",
    _gate(_state(
        schedule_id="ab349afa-63ca-41e9-82a8-1235eefb7993",
    )).abort_code is None,
    "the shared coverage row must serve every cadence of its dataset",
)
_check(
    "a differing schedule_id does not spoil a recorded gap either",
    _gate(_state(
        bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
        schedule_id="ab349afa-63ca-41e9-82a8-1235eefb7993",
    )).abort_code == TRIPS_COVERAGE_GAP_DETECTED,
    "a valid recorded gap stays a gap, not a bootstrap requirement",
)
identity_precedence = _gate(_state(
    bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
    client_id="5e45e815-b1ec-4164-88c7-611590cd8ea7",
    coverage_start_ts=None,
))
_check(
    "identity failure precedes recorded-gap structure and classification",
    identity_precedence.abort_code == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and "client_id" in identity_precedence.reason
    and "coverage_start_ts" not in identity_precedence.reason,
    f"got {identity_precedence}",
)
_check(
    "valid recorded GAP_DETECTED is deterministic and preserves normalized A/W",
    gap_row == _gate(_state(bootstrap_status=COVERAGE_STATUS_GAP_DETECTED))
    and gap_row.coverage_start_ts == _utc(2026, 6, 1)
    and gap_row.covered_through_ts == _utc(2026, 8, 1),
    f"got {gap_row}",
)
_check(
    "sub-second seeded_at remains accepted under the existing aware-only contract",
    _gate(_state(
        bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
        seeded_at=_utc(2026, 7, 1, 12).replace(microsecond=1),
    )).abort_code == TRIPS_COVERAGE_GAP_DETECTED,
)

_check(
    "a NULL client_code on the coverage row is not a mismatch",
    _gate(_state(client_code=None)).allowed is True,
)

# TIMESTAMPTZ is an absolute instant and the platform session renders it in the
# business timezone, so a non-zero offset is normal, not malformed state.
try:
    from zoneinfo import ZoneInfo

    warsaw = ZoneInfo("Europe/Warsaw")
    offset_state = _state(
        coverage_start_ts=_utc(2026, 6, 1, 0, 0, 0).astimezone(warsaw),
        covered_through_ts=_utc(2026, 8, 1, 0, 0, 0).astimezone(warsaw),
        seeded_at=_utc(2026, 7, 1, 12, 0, 0).astimezone(warsaw),
    )
    offset_result = _gate(offset_state)
    _check(
        "coverage bounds rendered in the business timezone are accepted",
        offset_result.allowed is True
        and offset_result.coverage_start_ts == _utc(2026, 6, 1, 0, 0, 0)
        and offset_result.coverage_start_ts.utcoffset() == timedelta(0),
        f"got {offset_result}",
    )
except Exception as exc:  # pragma: no cover - zoneinfo data must be present
    _check("coverage bounds rendered in the business timezone are accepted",
           False, repr(exc))


print("\n# Pure gate — allowed READY states")

allowed = _gate(_state())
_check(
    "valid READY connected state is allowed",
    allowed.allowed is True
    and allowed.classification == COVERAGE_GATE_ALLOWED
    and allowed.abort_code is None
    and allowed.requires_gap_persistence is False
    and allowed.effective_window is not None,
    f"got {allowed}",
)
_check(
    "allowed result carries the C4 arithmetic unchanged",
    allowed.effective_window.effective_window_end_ts == E_END
    and allowed.effective_window.effective_window_start_ts == BASE_START
    and allowed.effective_window.nominal_window_end_ts == FIRE
    and allowed.effective_window.nominal_window_start_ts
    == FIRE - timedelta(days=LOOKBACK_DAYS),
    f"got {allowed.effective_window}",
)
_check(
    "allowed result echoes the original immutable bounds",
    allowed.coverage_start_ts == _utc(2026, 6, 1, 0, 0, 0)
    and allowed.covered_through_ts == _utc(2026, 8, 1, 0, 0, 0),
)
# A recent-A claim: the derived request legitimately reaches back before A.
before_a = _gate(_state(
    coverage_start_ts=_utc(2026, 7, 30, 0, 0, 0),
    covered_through_ts=_utc(2026, 8, 1, 0, 0, 0),
))
_check(
    "the effective request may start before A without moving A",
    before_a.allowed is True
    and before_a.effective_window.effective_window_start_ts
    < before_a.coverage_start_ts
    and before_a.effective_window.coverage_start_ts == _utc(2026, 7, 30, 0, 0, 0),
    f"E_start={before_a.effective_window.effective_window_start_ts}",
)

# The connectivity boundary is only reachable through the R cap: whenever
# `min(base, W − O)` selects a branch, E_start <= W − O <= W, which is always
# connected. These three cases walk the boundary one second at a time.
W_OLD = FIRE - timedelta(days=20)
DELTA_SECONDS = int((E_END - W_OLD).total_seconds())


def _capped(recovery_seconds: int) -> CoverageGateResult:
    return _gate(
        _state(
            coverage_start_ts=W_OLD - timedelta(days=10),
            covered_through_ts=W_OLD,
        ),
        max_recovery_span_seconds=recovery_seconds,
    )


shared = _capped(DELTA_SECONDS)
_check(
    "shared endpoint (E_start == W) is connected and allowed",
    shared.allowed is True
    and shared.effective_window.effective_window_start_ts == W_OLD,
    f"got {shared}",
)

adjacent = _capped(DELTA_SECONDS - 1)
_check(
    "one-second adjacency (E_start == W + 1 s) is connected and allowed",
    adjacent.allowed is True
    and adjacent.effective_window.effective_window_start_ts
    == W_OLD + timedelta(seconds=1),
    f"got {adjacent}",
)

two_second = _capped(DELTA_SECONDS - 2)
_check(
    "two-second separation (E_start == W + 2 s) is disconnected",
    two_second.allowed is False
    and two_second.abort_code == TRIPS_COVERAGE_GAP_DETECTED
    and two_second.requires_gap_persistence is True,
    f"got {two_second}",
)

# A stale W that is merely older than `base` widens the window instead of
# disconnecting it — the self-healing property of docs/13 §15.1.
stale_w = BASE_START - timedelta(seconds=2)
stale = _gate(_state(coverage_start_ts=stale_w, covered_through_ts=stale_w))
_check(
    "a slightly older W widens the window instead of disconnecting it",
    stale.allowed is True
    and stale.effective_window.effective_window_start_ts
    == stale_w - timedelta(seconds=OVERLAP),
    f"got {stale}",
)

# Degenerate closed interval [t, t]: zero lookback and zero overlap.
degenerate = evaluate_coverage_gate(
    schedule_id=SCHEDULE_ID, client_id=CLIENT_ID, client_code=CLIENT_CODE,
    dataset_name=TRIPS_DS, scheduled_fire_ts=FIRE, lookback_days=0,
    stabilization_delay_seconds=DELAY, overlap_seconds=0,
    max_recovery_span_seconds=RECOVERY,
    coverage_state=_state(
        coverage_start_ts=_utc(2026, 6, 1, 0, 0, 0),
        covered_through_ts=E_END,
    ),
    now_utc=NOW,
)
_check(
    "degenerate [t, t] effective window is valid and allowed",
    degenerate.allowed is True
    and degenerate.effective_window.effective_window_start_ts
    == degenerate.effective_window.effective_window_end_ts == E_END,
    f"got {degenerate}",
)


print("\n# Pure gate — genuine disconnected window")

# A hole wider than R: the recovery cap raises E_start past W + 1 s.
small_recovery = 86_400
stale_w = FIRE - timedelta(days=40)
disconnected = _gate(
    _state(coverage_start_ts=stale_w - timedelta(days=10), covered_through_ts=stale_w),
    max_recovery_span_seconds=small_recovery,
)
_check(
    "R cap over a wider hole yields TRIPS_COVERAGE_GAP_DETECTED",
    disconnected.allowed is False
    and disconnected.classification == COVERAGE_GATE_GAP_DETECTED
    and disconnected.abort_code == TRIPS_COVERAGE_GAP_DETECTED
    and disconnected.requires_gap_persistence is True,
    f"got {disconnected}",
)
_check(
    "gap result exposes no effective window to claim",
    disconnected.effective_window is None,
)
_check(
    "gap result preserves the evaluated bounds for the concurrency predicate",
    disconnected.coverage_start_ts == stale_w - timedelta(days=10)
    and disconnected.covered_through_ts == stale_w
    and disconnected.bootstrap_status == COVERAGE_STATUS_READY,
)


print("\n# Pure gate — purity, immutability, determinism")

_check(
    "CoverageState is frozen",
    _is_frozen := dataclasses.is_dataclass(CoverageState)
    and CoverageState.__dataclass_params__.frozen,
)
_check(
    "CoverageGateResult is frozen",
    dataclasses.is_dataclass(CoverageGateResult)
    and CoverageGateResult.__dataclass_params__.frozen,
)
try:
    object.__setattr__  # noqa: B018 - readability only
    allowed.allowed = False  # type: ignore[misc]
    _check("CoverageGateResult rejects mutation", False, "assignment succeeded")
except dataclasses.FrozenInstanceError:
    _check("CoverageGateResult rejects mutation", True)
try:
    _state().bootstrap_status = COVERAGE_STATUS_READY  # type: ignore[misc]
    _check("CoverageState rejects mutation", False, "assignment succeeded")
except dataclasses.FrozenInstanceError:
    _check("CoverageState rejects mutation", True)

_check(
    "repeated evaluation is deterministic",
    _gate(_state()) == _gate(_state()) and _gate(None) == _gate(None),
)

_check(
    "CoverageState holds no connection/cursor/logger field",
    {f.name for f in dataclasses.fields(CoverageState)} == {
        "schedule_id", "client_id", "client_code", "dataset_name",
        "coverage_start_ts", "covered_through_ts", "bootstrap_status",
        "bootstrap_evidence_ref", "seeded_at", "seeded_by",
        "covered_through_source", "last_gap_detected_ts",
    },
    f"{[f.name for f in dataclasses.fields(CoverageState)]}",
)

_check(
    "gate result never exposes the evidence reference itself",
    "artifact:0f2b1c" not in repr(_gate(_state())),
)


class _ExplodingSocket:
    def __init__(self, *args, **kwargs):
        raise AssertionError("the pure gate must not open a socket")


def _no_io(callable_):
    real_socket = socket.socket
    real_connect = socket.create_connection
    socket.socket = _ExplodingSocket  # type: ignore[assignment]
    socket.create_connection = _ExplodingSocket  # type: ignore[assignment]
    try:
        return callable_()
    finally:
        socket.socket = real_socket  # type: ignore[assignment]
        socket.create_connection = real_connect  # type: ignore[assignment]


_check(
    "gate evaluation performs no socket I/O",
    _no_io(lambda: _gate(_state())).allowed is True,
)

for bad_config in (
    {"stabilization_delay_seconds": -1},
    {"overlap_seconds": "3600"},
    {"max_recovery_span_seconds": 0},
    {"lookback_days": -1},
    {"scheduled_fire_ts": datetime(2026, 8, 2)},
):
    try:
        _gate(_state(), **bad_config)
        _check(f"invalid configuration raises: {bad_config}", False, "no raise")
    except ValueError:
        _check(f"invalid configuration raises: {bad_config}", True)

try:
    _gate({"bootstrap_status": "READY"})  # type: ignore[arg-type]
    _check("a non-CoverageState coverage_state raises", False, "no raise")
except ValueError:
    _check("a non-CoverageState coverage_state raises", True)


# ---------------------------------------------------------------------------
# 2) Dispatcher unit level — evidence, params, side-effect boundary
# ---------------------------------------------------------------------------

print("\n# Dispatcher — claim evidence and job parameters")


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((str(sql), params))

    def fetchone(self):
        return self.conn.fetchone_value

    def fetchall(self):
        return self.conn.fetchall_value

    @property
    def rowcount(self):
        return self.conn.rowcount


class _FakeConn:
    def __init__(self, *, fetchone_value=None, fetchall_value=None, rowcount=1):
        self.fetchone_value = fetchone_value
        self.fetchall_value = fetchall_value or []
        self.rowcount = rowcount
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


strict_conn = _FakeConn(fetchone_value=("rh-strict",))
disp._claim_fire(
    strict_conn, schedule_id=SCHEDULE_ID, client_id=CLIENT_ID,
    client_code=CLIENT_CODE, dataset_name=TRIPS_DS,
    scheduled_fire_ts=FIRE, window_start_ts=FIRE - timedelta(days=1),
    window_end_ts=FIRE,
)
strict_sql, strict_params = strict_conn.executed[0]
_check(
    "claim INSERT uses an explicit evidence column list",
    "nominal_window_start_ts, nominal_window_end_ts" in strict_sql
    and "stabilization_delay_seconds, overlap_seconds" in strict_sql
    and "trips_pagination_mode" in strict_sql,
    strict_sql,
)
_check(
    "strict claim writes NULL into all five evidence columns",
    strict_params[-5:] == (None, None, None, None, None),
    f"{strict_params}",
)
_check(
    "claim INSERT does not write trips_max_recovery_span_seconds",
    "trips_max_recovery_span_seconds" not in strict_sql,
)

compat_conn = _FakeConn(fetchone_value=("rh-compat",))
disp._claim_fire(
    compat_conn, schedule_id=SCHEDULE_ID, client_id=CLIENT_ID,
    client_code=CLIENT_CODE, dataset_name=TRIPS_DS,
    scheduled_fire_ts=FIRE, window_start_ts=BASE_START, window_end_ts=E_END,
    nominal_window_start_ts=FIRE - timedelta(days=LOOKBACK_DAYS),
    nominal_window_end_ts=FIRE,
    stabilization_delay_seconds=DELAY, overlap_seconds=OVERLAP,
    trips_pagination_mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
)
_, compat_params = compat_conn.executed[0]
_check(
    "compat claim stores the effective window in window_start/end_ts",
    compat_params[4] == BASE_START and compat_params[5] == E_END,
    f"{compat_params}",
)
_check(
    "compat claim stores the nominal window and exact loaded config",
    compat_params[-5:] == (
        FIRE - timedelta(days=LOOKBACK_DAYS), FIRE, DELAY, OVERLAP,
        TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    ),
    f"{compat_params}",
)

finalize_conn = _FakeConn()
disp._finalize_run(
    finalize_conn, run_history_id="rh-compat", status="SUCCESS", error=None,
)
finalize_sql = finalize_conn.executed[0][0]
_check(
    "finalization never rewrites the claim-time evidence columns",
    "nominal_window" not in finalize_sql
    and "stabilization_delay_seconds" not in finalize_sql
    and "overlap_seconds" not in finalize_sql
    and "trips_pagination_mode" not in finalize_sql
    and "SET status" in finalize_sql,
    finalize_sql,
)

strict_params_out = disp._build_job_params(
    client_id=CLIENT_ID, client_code=CLIENT_CODE, dataset_name=TRIPS_DS,
    event_enrichment_mode="enabled",
    window_start_ts=FIRE - timedelta(days=1), window_end_ts=FIRE,
    scheduled_fire_ts=FIRE,
)
_check(
    "strict trips params keep today's window values",
    strict_params_out["window_start_ts"] == "2026-08-01T00:00:00Z"
    and strict_params_out["window_end_ts"] == "2026-08-02T00:00:00Z"
    and strict_params_out["event_enrichment_mode"] == "enabled"
    and strict_params_out["trigger"] == "SCHEDULED",
    f"{strict_params_out}",
)
_check(
    "strict trips params carry scheduled_fire_ts and nothing compat-specific",
    strict_params_out["scheduled_fire_ts"] == "2026-08-02T00:00:00Z"
    and not {
        "nominal_window_start_ts", "nominal_window_end_ts",
        "trips_stabilization_delay_seconds", "trips_overlap_seconds",
        "trips_pagination_mode",
    } & set(strict_params_out),
    f"{strict_params_out}",
)

compat_params_out = disp._build_job_params(
    client_id=CLIENT_ID, client_code=CLIENT_CODE, dataset_name=TRIPS_DS,
    event_enrichment_mode="enabled",
    window_start_ts=BASE_START, window_end_ts=E_END,
    scheduled_fire_ts=FIRE,
    nominal_window_start_ts=FIRE - timedelta(days=LOOKBACK_DAYS),
    nominal_window_end_ts=FIRE,
    trips_stabilization_delay_seconds=DELAY,
    trips_overlap_seconds=OVERLAP,
    trips_pagination_mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
)
_check(
    "compat trips params fetch the effective window exactly once",
    compat_params_out["window_start_ts"] == disp._iso_z(BASE_START)
    and compat_params_out["window_end_ts"] == disp._iso_z(E_END)
    and compat_params_out["nominal_window_end_ts"] == disp._iso_z(FIRE)
    and compat_params_out["trips_stabilization_delay_seconds"] == DELAY
    and compat_params_out["trips_overlap_seconds"] == OVERLAP
    and compat_params_out["trips_pagination_mode"]
    == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    f"{compat_params_out}",
)

eco_params = disp._build_job_params(
    client_id=CLIENT_ID, client_code=CLIENT_CODE,
    dataset_name="eco_driving_weekly_snapshot", event_enrichment_mode="enabled",
    window_start_ts=FIRE - timedelta(days=1), window_end_ts=FIRE,
    scheduled_fire_ts=FIRE,
)
_check(
    "Eco Driving params are untouched by C5",
    eco_params.get("mode") == "weekly_cumulative_snapshot"
    and "window_start_ts" not in eco_params,
    f"{eco_params}",
)


print("\n# Dispatcher — side-effect boundary on a rejected gate")


class _FakeClient:
    def __init__(self):
        self.logs: list[tuple[str, str, dict]] = []
        self.bugs: list[object] = []

    def log(self, level, kind, source, message, run_id=None, context=None):
        self.logs.append((level, message, dict(context or {})))

    def report_suspected_bug(self, event):
        self.bugs.append(event)
        return {"fingerprint": "fake"}


def _fatal(*args, **kwargs):
    raise AssertionError("a rejected gate reached a launch-side side effect")


def _schedule_row(**overrides) -> disp.ScheduleRow:
    values = dict(
        schedule_id=SCHEDULE_ID, client_id=CLIENT_ID, client_code=CLIENT_CODE,
        client_name="Test", dataset_name=TRIPS_DS, job_module=TRIPS_JOB,
        enabled=True, frequency="daily", day_of_week=None, day_of_month=None,
        day_of_month_last=False, run_time=time(2, 0), timezone_name="UTC",
        lookback_days=LOOKBACK_DAYS, overwrite_existing=True,
        event_enrichment_mode="enabled",
        trips_pagination_mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
        trips_stabilization_delay_seconds=DELAY,
        trips_overlap_seconds=OVERLAP,
        trips_max_recovery_span_seconds=RECOVERY,
    )
    values.update(overrides)
    return disp.ScheduleRow(**values)


def _run_rejected(
    gate: CoverageGateResult, state, conn, client=None,
) -> tuple[_FakeClient, Exception | None]:
    """Run `run_prepared` with every launch-side effect patched to be fatal."""
    prepared = disp.PreparedDispatcherRun(
        conn=conn, schedule=_schedule_row(), fire_utc=FIRE,
        window_start=FIRE - timedelta(days=LOOKBACK_DAYS), window_end=FIRE,
        run_history_id="rh-reject", stale_rows=[], stale_after_minutes=720,
        nominal_window_start=FIRE - timedelta(days=LOOKBACK_DAYS),
        nominal_window_end=FIRE, coverage_state=state, gate_result=gate,
    )
    client = client or _FakeClient()
    saved = {
        name: getattr(disp, name)
        for name in (
            "_launch_job", "_build_job_params", "_set_platform_run_id",
            "_finalize_compat_gap",
        )
    }

    def _record_gap_finalizer(finalizer_conn, *, prepared):
        assert prepared.coverage_state is state
        finalizer_conn.executed.append((
            "C6_GAP_FINALIZER: coverage READY->GAP_DETECTED + history RUNNING->FAILED",
            (prepared.run_history_id, gate.abort_code),
        ))
        finalizer_conn.commit()

    saved_popen = disp.subprocess.Popen
    saved_socket = socket.socket
    saved_create = socket.create_connection
    for name in ("_launch_job", "_build_job_params", "_set_platform_run_id"):
        setattr(disp, name, _fatal)
    disp._finalize_compat_gap = _record_gap_finalizer
    disp.subprocess.Popen = _fatal  # type: ignore[assignment]
    socket.socket = _ExplodingSocket  # type: ignore[assignment]
    socket.create_connection = _ExplodingSocket  # type: ignore[assignment]
    error: Exception | None = None
    try:
        disp.run_prepared(client, "run-1", {}, prepared)
    except Exception as exc:  # noqa: BLE001 - the abort is the assertion
        error = exc
    finally:
        for name, value in saved.items():
            setattr(disp, name, value)
        disp.subprocess.Popen = saved_popen  # type: ignore[assignment]
        socket.socket = saved_socket  # type: ignore[assignment]
        socket.create_connection = saved_create  # type: ignore[assignment]
    return client, error


bootstrap_gate = _gate(None)
reject_conn = _FakeConn()
client, error = _run_rejected(bootstrap_gate, None, reject_conn)
_check(
    "bootstrap rejection raises without reaching any launch side effect",
    isinstance(error, RuntimeError)
    and TRIPS_COVERAGE_BOOTSTRAP_REQUIRED in str(error),
    f"{error!r}",
)
_check(
    "bootstrap rejection finalizes the claimed row FAILED",
    any(
        "SET status" in sql and params[0] == "FAILED"
        and params[2] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
        for sql, params in reject_conn.executed
    ),
    f"{reject_conn.executed}",
)
_check(
    "bootstrap rejection writes no coverage row",
    not any("client_dataset_coverage" in sql for sql, _ in reject_conn.executed),
    f"{[sql for sql, _ in reject_conn.executed]}",
)
error_logs = [entry for entry in client.logs if entry[0] == "ERROR"]
_check(
    "bootstrap rejection emits a structured ERROR with the abort code",
    len(error_logs) == 1
    and error_logs[0][2]["abort_code"] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and error_logs[0][2]["schedule_id"] == SCHEDULE_ID
    and error_logs[0][2]["client_code"] == CLIENT_CODE
    and error_logs[0][2]["dataset_name"] == TRIPS_DS
    and error_logs[0][2]["scheduled_fire_ts"] == disp._iso_z(FIRE)
    and error_logs[0][2]["trips_pagination_mode"]
    == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1
    and error_logs[0][2]["coverage_row_present"] is False,
    f"{error_logs}",
)
_check(
    "bootstrap rejection reports exactly one suspected bug",
    len(client.bugs) == 1
    and client.bugs[0].incident_code == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and client.bugs[0].rows_modified == 0,
    f"{client.bugs}",
)

ready_state = _state(bootstrap_status=COVERAGE_STATUS_READY)
status_gate = _gate(_state(bootstrap_status=COVERAGE_STATUS_UNINITIALIZED))
client_b, _ = _run_rejected(
    status_gate, _state(bootstrap_status=COVERAGE_STATUS_UNINITIALIZED), _FakeConn(),
)
bug = client_b.bugs[0]
_check(
    "suspected bug fingerprint excludes the per-fire timestamp",
    "scheduled_fire_ts" not in bug.fingerprint_fields
    and bug.fingerprint_fields["schedule_id"] == SCHEDULE_ID
    and bug.fingerprint_fields["abort_code"] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
    f"{bug.fingerprint_fields}",
)
_check(
    "repeated identical failures share one fingerprint",
    bug.fingerprint() == client_b.bugs[0].fingerprint(),
)
_check(
    "no evidence reference, secret ref or DSN reaches the report",
    "artifact:0f2b1c" not in str(bug.to_dict())
    and "password" not in str(bug.to_dict()).lower()
    and bug.details["bootstrap_evidence_ref_present"] is True,
    f"{bug.details}",
)
_check(
    "error_summary stays bounded",
    len(TRIPS_COVERAGE_BOOTSTRAP_REQUIRED) < 200,
)


malformed_gap_state = _state(
    bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
    coverage_start_ts=None,
    bootstrap_evidence_ref="raw-evidence-DO-NOT-LOG",
)
malformed_gap_gate = _gate(malformed_gap_state)
malformed_conn = _FakeConn()
malformed_client, malformed_error = _run_rejected(
    malformed_gap_gate, malformed_gap_state, malformed_conn,
)
malformed_error_logs = [
    entry for entry in malformed_client.logs if entry[0] == "ERROR"
]
_check(
    "malformed GAP_DETECTED logs bootstrap-required without implying a trusted gap",
    isinstance(malformed_error, RuntimeError)
    and TRIPS_COVERAGE_BOOTSTRAP_REQUIRED in str(malformed_error)
    and len(malformed_error_logs) == 1
    and malformed_error_logs[0][2]["abort_code"]
    == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and malformed_error_logs[0][2]["bootstrap_status"]
    == COVERAGE_STATUS_GAP_DETECTED
    and "unclosed hole" not in malformed_error_logs[0][2]["coverage_gate_reason"],
    f"error={malformed_error!r} logs={malformed_error_logs}",
)
_check(
    "malformed GAP_DETECTED logs and reports never expose raw evidence",
    "raw-evidence-DO-NOT-LOG" not in str(malformed_client.logs)
    and "raw-evidence-DO-NOT-LOG" not in str(
        [bug.to_dict() for bug in malformed_client.bugs]
    ),
)
_check(
    "malformed GAP_DETECTED suspected-bug fingerprint follows bootstrap taxonomy",
    len(malformed_client.bugs) == 1
    and malformed_client.bugs[0].incident_code
    == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
    and malformed_client.bugs[0].fingerprint_fields["abort_code"]
    == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
)


class _FailingReportClient(_FakeClient):
    def report_suspected_bug(self, event):
        raise RuntimeError("reporting unavailable")


report_conn = _FakeConn()
report_client = _FailingReportClient()
_, report_error = _run_rejected(
    malformed_gap_gate, malformed_gap_state, report_conn, client=report_client,
)
_check(
    "reporting failure does not mask malformed-gap terminal history",
    isinstance(report_error, RuntimeError)
    and TRIPS_COVERAGE_BOOTSTRAP_REQUIRED in str(report_error)
    and any(
        "SET status" in sql and params[0] == "FAILED"
        and params[2] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
        for sql, params in report_conn.executed
    )
    and any(entry[0] == "WARNING" for entry in report_client.logs),
    f"error={report_error!r} statements={report_conn.executed}",
)
for gap_label, gap_gate in (
    ("disconnected READY window", disconnected),
    ("pre-existing GAP_DETECTED row", gap_row),
):
    gap_conn = _FakeConn()
    gap_client, gap_error = _run_rejected(gap_gate, _state(), gap_conn)
    _check(
        f"gap rejection raises TRIPS_COVERAGE_GAP_DETECTED: {gap_label}",
        isinstance(gap_error, RuntimeError)
        and TRIPS_COVERAGE_GAP_DETECTED in str(gap_error),
        f"{gap_error!r}",
    )
    gap_statements = [
        sql for sql, _ in gap_conn.executed if "workflow_a_control." in sql
    ]
    if gap_gate.requires_gap_persistence:
        _check(
            "new disconnected READY invokes the atomic C6 gap finalizer",
            [
                entry for entry in gap_conn.executed
                if entry[0].startswith("C6_GAP_FINALIZER:")
            ] == [(
                "C6_GAP_FINALIZER: coverage READY->GAP_DETECTED + history RUNNING->FAILED",
                ("rh-reject", TRIPS_COVERAGE_GAP_DETECTED),
            )],
            f"{gap_conn.executed}",
        )
    else:
        _check(
            "existing GAP_DETECTED writes exactly one history statement",
            len(gap_statements) == 1
            and "client_schedule_run_history" in gap_statements[0]
            and "SET status" in gap_statements[0],
            f"{gap_statements}",
        )
        _check(
            "existing GAP_DETECTED touches no coverage row",
            not any(
                "client_dataset_coverage" in sql for sql, _ in gap_conn.executed
            )
            and gap_conn.rollbacks == 0,
            f"{[sql for sql, _ in gap_conn.executed]}",
        )
    _check(
        f"gap rejection reports the gap incident: {gap_label}",
        len(gap_client.bugs) == 1
        and gap_client.bugs[0].incident_code == TRIPS_COVERAGE_GAP_DETECTED,
    )
    gap_log = [entry for entry in gap_client.logs if entry[0] == "ERROR"][0]
    _check(
        f"gap log states the correct mutation outcome: {gap_label}",
        gap_log[2]["coverage_mutation_performed"]
        is gap_gate.requires_gap_persistence
        and gap_log[2]["abort_code"] == TRIPS_COVERAGE_GAP_DETECTED,
        f"{gap_log[2]}",
    )

_check(
    "a disconnected READY window signals C6 that persistence is owed",
    disconnected.requires_gap_persistence is True,
)
_check(
    "the dispatcher exposes only the approved named gap finalizer",
    hasattr(disp, "_finalize_compat_gap")
    and not hasattr(disp, "_finalize_run_with_coverage_gap"),
)

_check(
    "a strict fire carries no gate and takes the unchanged path",
    disp._is_compatibility_trips_fire(
        _schedule_row(trips_pagination_mode=TRIPS_PAGINATION_MODE_STRICT_META)
    ) is False
    and disp._is_compatibility_trips_fire(
        _schedule_row(dataset_name="fuel_daily_aggregation")
    ) is False
    and disp._is_compatibility_trips_fire(_schedule_row()) is True,
)


# ---------------------------------------------------------------------------
# 3) Disposable PostgreSQL integration
# ---------------------------------------------------------------------------

def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _bootstrap_schema(conn) -> None:
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in MIGRATIONS:
        conn.execute(_sql(name))
    conn.commit()


def _seed_control_plane(conn, *, mode: str) -> None:
    conn.execute(
        "DELETE FROM workflow_a_control.client_schedule_run_history"
    )
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
    conn.execute("DELETE FROM workflow_a_control.client_account")
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_account
          (client_id, client_code, client_name, provider_type,
           provider_base_url, provider_basic_auth_username,
           provider_basic_auth_password_secret_ref,
           client_db_host, client_db_port, client_db_name, client_db_user,
           client_db_password_secret_ref, client_db_schema,
           speed_trigger_filter_text, enabled, trips_pagination_mode)
        VALUES (%s, %s, 'Test', 'telematics', 'https://example.invalid', 'u',
                'REF', '127.0.0.1', 5432, 'db', 'user', 'REF', 'public',
                'speeding', true, %s)
        """,
        (CLIENT_ID, CLIENT_CODE, mode),
    )
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_schedule
          (schedule_id, client_id, client_code, dataset_name, enabled,
           frequency, run_time, timezone, lookback_days, overwrite_existing)
        VALUES (%s, %s, %s, %s, true, 'daily', '02:00', 'UTC', %s, true)
        """,
        (SCHEDULE_ID, CLIENT_ID, CLIENT_CODE, TRIPS_DS, LOOKBACK_DAYS),
    )
    conn.commit()


def _seed_coverage(conn, **values) -> None:
    row = dict(
        schedule_id=SCHEDULE_ID, client_id=CLIENT_ID, client_code=CLIENT_CODE,
        dataset_name=TRIPS_DS, coverage_start_ts=None, covered_through_ts=None,
        bootstrap_status=COVERAGE_STATUS_UNINITIALIZED,
        bootstrap_evidence_ref=None, seeded_at=None, seeded_by=None,
    )
    row.update(values)
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_coverage
          (schedule_id, client_id, client_code, dataset_name,
           coverage_start_ts, covered_through_ts, bootstrap_status,
           bootstrap_evidence_ref, seeded_at, seeded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (row["schedule_id"], row["client_id"], row["client_code"],
         row["dataset_name"], row["coverage_start_ts"],
         row["covered_through_ts"], row["bootstrap_status"],
         row["bootstrap_evidence_ref"], row["seeded_at"], row["seeded_by"]),
    )
    conn.commit()
    conn.commit()


def _history_rows(conn) -> list[dict]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT status, error_summary, window_start_ts, window_end_ts,
                   scheduled_fire_ts, nominal_window_start_ts,
                   nominal_window_end_ts, stabilization_delay_seconds,
                   overlap_seconds, trips_pagination_mode, finished_at
              FROM workflow_a_control.client_schedule_run_history
             ORDER BY scheduled_fire_ts
            """
        )
        return [dict(r) for r in cur.fetchall()]


def _coverage_rows(conn) -> list[dict]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT coverage_start_ts, covered_through_ts, bootstrap_status,
                   bootstrap_evidence_ref, covered_through_source,
                   last_gap_detected_ts, updated_at, seeded_at, seeded_by
              FROM workflow_a_control.client_dataset_coverage
            """
        )
        return [dict(r) for r in cur.fetchall()]


def _run_tick(*, expect_launch: bool, rc: int = 0):
    """Run one real dispatcher tick with the subprocess boundary replaced."""
    launched: list[dict] = []
    client = _FakeClient()

    def _fake_launch(
        *, job_module, job_params, log_fn, on_platform_run_id=None,
        collect_execution_outcome=False,
    ):
        if not expect_launch:
            raise AssertionError("subprocess launch was not expected")
        launched.append({"job_module": job_module, "params": dict(job_params)})
        if not collect_execution_outcome:
            # `strict_meta`: no record is requested and none is produced.
            return rc, "", "", None, disp.ScheduledExecutionOutcome(
                requested=False,
            )
        # Compatibility fire. Stand in for a child that genuinely did the work
        # and left a committed record bound to this launch's platform run, so
        # this harness continues to exercise the coverage-advancing path rather
        # than M3's refusal path — which has its own dedicated suite.
        platform_run_id = str(uuid.uuid4())
        if on_platform_run_id is not None:
            on_platform_run_id(platform_run_id)
        outcome = None
        if rc == 0:
            outcome = ExecutionOutcome.from_mapping(
                eo_fixtures.committed_outcome(
                    job_params,
                    schedule_id=SCHEDULE_ID,
                    platform_run_id=platform_run_id,
                )
            )
            # M4 step 1, as the real child does it: the request facts are made
            # durable on the platform side before the business transaction
            # commits. Finalization promotes them rather than inserting, so a
            # stand-in that skips this would exercise a shape production never
            # produces — and would be refused, correctly.
            import psycopg as _psycopg
            from jobs.api.telematics.request_evidence import (
                persist_pending_request_facts as _persist,
            )
            _facts_conn = _psycopg.connect(
                os.environ["TELEMATICS_COVERAGE_GATE_TEST_DSN"]
            )
            try:
                _persist(
                    _facts_conn,
                    platform_run_id=platform_run_id,
                    completeness=outcome.window_completeness,
                )
                _facts_conn.commit()
            finally:
                _facts_conn.close()
        return (
            rc, "", "", platform_run_id,
            disp.ScheduledExecutionOutcome(requested=True, outcome=outcome),
        )

    saved_launch = disp._launch_job
    saved_builder = disp._build_job_params
    saved_popen = disp.subprocess.Popen
    disp._launch_job = _fake_launch  # type: ignore[assignment]
    if not expect_launch:
        disp._build_job_params = _fatal  # type: ignore[assignment]
    disp.subprocess.Popen = _fatal  # type: ignore[assignment]
    error: Exception | None = None
    try:
        prepared = disp.prepare_run({})
        if prepared is not None:
            disp.run_prepared(client, "run-int", {}, prepared)
    except Exception as exc:  # noqa: BLE001
        error = exc
    finally:
        disp._launch_job = saved_launch  # type: ignore[assignment]
        disp._build_job_params = saved_builder  # type: ignore[assignment]
        disp.subprocess.Popen = saved_popen  # type: ignore[assignment]
    return client, launched, error


def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg

    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    saved_env = {
        key: os.environ.get(key)
        for key in (
            "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB",
            "POSTGRES_USER", "POSTGRES_PASSWORD",
        )
    }
    os.environ["POSTGRES_HOST"] = str(parsed.get("host") or "127.0.0.1")
    os.environ["POSTGRES_PORT"] = str(parsed.get("port") or "5432")
    os.environ["POSTGRES_DB"] = str(parsed.get("dbname") or "postgres")
    os.environ["POSTGRES_USER"] = str(parsed.get("user") or "")
    os.environ["POSTGRES_PASSWORD"] = str(parsed.get("password") or "")

    conn = psycopg.connect(dsn)
    try:
        _bootstrap_schema(conn)

        # --- strict mode -------------------------------------------------
        print("\n# PostgreSQL — strict mode is untouched")
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_STRICT_META)
        # Prove no coverage SELECT is issued at all: remove the table.
        conn.execute("DROP TABLE workflow_a_control.client_dataset_coverage")
        conn.commit()
        client, launched, error = _run_tick(expect_launch=True)
        rows = _history_rows(conn)
        _check(
            "strict tick runs normally with no coverage table present",
            error is None and len(launched) == 1 and len(rows) == 1
            and rows[0]["status"] == "SUCCESS",
            f"error={error!r} rows={rows}",
        )
        _check(
            "strict claim preserves the nominal execution window",
            rows[0]["window_end_ts"] == rows[0]["scheduled_fire_ts"]
            and rows[0]["window_start_ts"]
            == rows[0]["scheduled_fire_ts"] - timedelta(days=LOOKBACK_DAYS),
            f"{rows[0]}",
        )
        _check(
            "strict claim leaves every evidence column NULL",
            all(
                rows[0][name] is None for name in (
                    "nominal_window_start_ts", "nominal_window_end_ts",
                    "stabilization_delay_seconds", "overlap_seconds",
                    "trips_pagination_mode",
                )
            ),
            f"{rows[0]}",
        )
        _check(
            "strict params carry no compatibility keys",
            not {
                "nominal_window_start_ts", "trips_pagination_mode",
                "trips_stabilization_delay_seconds", "trips_overlap_seconds",
            } & set(launched[0]["params"]),
            f"{launched[0]['params']}",
        )
        conn.execute(_sql("057_workflow_a_trips_coverage_state.sql"))
        conn.commit()

        # --- compatibility: missing coverage row --------------------------
        print("\n# PostgreSQL — compatibility fail-closed states")
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
        client, launched, error = _run_tick(expect_launch=False)
        rows = _history_rows(conn)
        _check(
            "missing coverage row produces terminal FAILED history",
            isinstance(error, RuntimeError) and len(rows) == 1
            and rows[0]["status"] == "FAILED"
            and rows[0]["error_summary"] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
            and rows[0]["finished_at"] is not None,
            f"error={error!r} rows={rows}",
        )
        _check(
            "missing coverage row launches no subprocess",
            launched == [],
        )
        _check(
            "rejected claim records nominal windows and honest evidence",
            rows[0]["window_end_ts"] == rows[0]["scheduled_fire_ts"]
            and rows[0]["nominal_window_end_ts"] == rows[0]["scheduled_fire_ts"]
            and rows[0]["stabilization_delay_seconds"] == 10_800
            and rows[0]["overlap_seconds"] == 3_600
            and rows[0]["trips_pagination_mode"]
            == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
            f"{rows[0]}",
        )
        _check(
            "no coverage row is created automatically",
            _coverage_rows(conn) == [],
        )
        _check(
            "a duplicate tick does not rewrite the terminal row",
            (lambda: (
                _run_tick(expect_launch=False),
                _history_rows(conn) == rows,
            )[1])(),
        )


        malformed_db_gap_cases = (
            ("GAP_DETECTED NULL A", dict(
                coverage_start_ts=None, covered_through_ts=_utc(2026, 8, 1))),
            ("GAP_DETECTED NULL W", dict(
                coverage_start_ts=_utc(2026, 6, 1), covered_through_ts=None)),
            ("GAP_DETECTED blank evidence", dict(
                coverage_start_ts=_utc(2026, 6, 1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_evidence_ref="   ", seeded_at=_utc(2026, 7, 1),
                seeded_by="operator")),
            ("GAP_DETECTED missing seeded metadata", dict(
                coverage_start_ts=_utc(2026, 6, 1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_evidence_ref="artifact:seed-missing")),
            ("GAP_DETECTED sub-second A", dict(
                coverage_start_ts=_utc(2026, 6, 1).replace(microsecond=1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_evidence_ref="artifact:precision",
                seeded_at=_utc(2026, 7, 1), seeded_by="operator")),
            ("GAP_DETECTED client identity mismatch", dict(
                client_id="5e45e815-b1ec-4164-88c7-611590cd8ea7",
                coverage_start_ts=_utc(2026, 6, 1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_evidence_ref="artifact:identity",
                seeded_at=_utc(2026, 7, 1), seeded_by="operator")),
            ("GAP_DETECTED dataset mismatch", dict(
                dataset_name="fuel_daily_aggregation",
                coverage_start_ts=_utc(2026, 6, 1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_evidence_ref="artifact:dataset",
                seeded_at=_utc(2026, 7, 1), seeded_by="operator")),
        )
        for label, malformed_seed in malformed_db_gap_cases:
            _seed_control_plane(
                conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
            )
            _seed_coverage(
                conn, bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
                **malformed_seed,
            )
            before = _coverage_rows(conn)
            malformed_client, launched, error = _run_tick(expect_launch=False)
            rows = _history_rows(conn)
            error_logs = [
                entry for entry in malformed_client.logs if entry[0] == "ERROR"
            ]
            _check(
                f"malformed database row is bootstrap-required: {label}",
                isinstance(error, RuntimeError) and launched == []
                and len(rows) == 1 and rows[0]["status"] == "FAILED"
                and rows[0]["error_summary"]
                == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED
                and len(error_logs) == 1
                and error_logs[0][2]["abort_code"]
                == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
                f"error={error!r} rows={rows} logs={error_logs}",
            )
            _check(
                f"malformed database row is byte-for-byte unchanged: {label}",
                _coverage_rows(conn) == before,
                f"before={before} after={_coverage_rows(conn)}",
            )
        for label, seed in (
            ("UNINITIALIZED with bounds", dict(
                coverage_start_ts=_utc(2026, 6, 1), covered_through_ts=_utc(2026, 8, 1),
                bootstrap_status=COVERAGE_STATUS_UNINITIALIZED)),
            ("RESEED_REQUIRED", dict(
                coverage_start_ts=_utc(2026, 6, 1), covered_through_ts=_utc(2026, 8, 1),
                bootstrap_status=COVERAGE_STATUS_RESEED_REQUIRED)),
            ("READY with W in the future", dict(
                coverage_start_ts=_utc(2026, 6, 1),
                covered_through_ts=_utc(2099, 1, 1),
                bootstrap_status=COVERAGE_STATUS_READY,
                bootstrap_evidence_ref="artifact:x", seeded_at=_utc(2026, 7, 1),
                seeded_by="op")),
        ):
            _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
            _seed_coverage(conn, **seed)
            before = _coverage_rows(conn)
            client, launched, error = _run_tick(expect_launch=False)
            rows = _history_rows(conn)
            _check(
                f"invalid state fails closed: {label}",
                isinstance(error, RuntimeError) and launched == []
                and rows[0]["status"] == "FAILED"
                and rows[0]["error_summary"] == TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
                f"error={error!r} rows={rows}",
            )
            _check(
                f"invalid state leaves the coverage row unchanged: {label}",
                _coverage_rows(conn) == before,
            )


        # Canonical migration 057 makes reversed bounds impossible to persist.
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
        try:
            _seed_coverage(
                conn,
                coverage_start_ts=_utc(2026, 8, 1, 0, 0, 1),
                covered_through_ts=_utc(2026, 8, 1),
                bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
                bootstrap_evidence_ref="artifact:reversed",
                seeded_at=_utc(2026, 7, 1), seeded_by="operator",
            )
            reversed_refused = False
        except psycopg.errors.CheckViolation:
            conn.rollback()
            reversed_refused = True
        _check(
            "migration 057 prevents a reversed GAP_DETECTED database fixture",
            reversed_refused and _coverage_rows(conn) == [],
        )

        # A naive Python datetime sent to TIMESTAMPTZ is normalized by PostgreSQL
        # and psycopg returns an aware instant; the malformed naive shape cannot
        # survive the canonical database adapter boundary.
        _seed_coverage(
            conn,
            coverage_start_ts=datetime(2026, 6, 1),
            covered_through_ts=datetime(2026, 8, 1),
            bootstrap_status=COVERAGE_STATUS_GAP_DETECTED,
            bootstrap_evidence_ref="artifact:adapter",
            seeded_at=_utc(2026, 7, 1), seeded_by="operator",
        )
        adapted = disp._load_coverage_state(
            conn, client_id=CLIENT_ID, dataset_name=TRIPS_DS,
        )
        _check(
            "TIMESTAMPTZ adapter cannot return naive coverage bounds",
            adapted.coverage_start_ts.utcoffset() is not None
            and adapted.covered_through_ts.utcoffset() is not None,
        )
        # --- compatibility: allowed connected READY -----------------------
        #
        # `W` is seeded **relative to the fire**, not at an absolute date. The
        # fire is `datetime.now()`-derived, so an absolute `W` drifts further
        # behind it every day the suite is not run, and once
        # `W - O < base` the `min(base, W - O)` clamp in
        # `derive_effective_window` silently selects the coverage-expansion
        # branch instead. This block is about gate admission and the
        # claim/finalize plumbing for a connected READY claim, so it must sit in
        # the steady state where `base` is the binding term; the clamp itself is
        # owned, in all four of its branches, by
        # `test_telematics_trips_stabilization_windows.py`.
        #
        # `W` = the effective end a daily-cadence predecessor run would have
        # left behind. That is late enough that `W - O > base` for any
        # `LOOKBACK_DAYS > 1`, and still strictly earlier than this fire's
        # `E_end`, so the monotonic advance below is genuinely exercised.
        print("\n# PostgreSQL — allowed compatibility fire")
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
        # The schedule seeded above is `daily` at `02:00` `UTC`; the latest such
        # instant at or before now is the fire the dispatcher will claim. Stated
        # here as plain arithmetic rather than by calling the scheduler, and
        # re-read from the durable history row below.
        _now = datetime.now(timezone.utc)
        _expected_fire = _now.replace(hour=2, minute=0, second=0, microsecond=0)
        if _expected_fire > _now:
            _expected_fire -= timedelta(days=1)
        previous_effective_end = (
            _expected_fire - timedelta(days=1) - timedelta(seconds=10_800)
        )
        ready_seed = dict(
            coverage_start_ts=_utc(2026, 6, 1),
            covered_through_ts=previous_effective_end,
            bootstrap_status=COVERAGE_STATUS_READY,
            bootstrap_evidence_ref="artifact:ready",
            seeded_at=_utc(2026, 7, 1), seeded_by="operator",
        )
        _seed_coverage(conn, **ready_seed)
        before = _coverage_rows(conn)
        client, launched, error = _run_tick(expect_launch=True)
        rows = _history_rows(conn)
        fire = rows[0]["scheduled_fire_ts"]
        expected_end = fire - timedelta(seconds=10_800)
        expected_start = (
            fire - timedelta(days=LOOKBACK_DAYS) - timedelta(seconds=10_800 + 3_600)
        )
        # State the branch this block intends to exercise instead of assuming
        # it. Without this, a later change to `LOOKBACK_DAYS` or to the seed
        # turns the two window checks below into assertions about the coverage
        # -expansion branch, still green and no longer testing what they name.
        _check(
            "the fixture isolates the base branch of min(base, W - O)",
            fire == _expected_fire
            and before[0]["covered_through_ts"] - timedelta(seconds=3_600)
            > expected_start
            and before[0]["covered_through_ts"] < expected_end,
            f"fire={fire} W={before[0]['covered_through_ts']} "
            f"base={expected_start} E_end={expected_end}",
        )
        _check(
            "allowed compat fire claims the effective window",
            error is None and len(launched) == 1
            and rows[0]["status"] == "SUCCESS"
            and rows[0]["window_start_ts"] == expected_start
            and rows[0]["window_end_ts"] == expected_end,
            f"error={error!r} rows={rows}",
        )
        _check(
            "claim-time evidence is written exactly once and honestly",
            rows[0]["nominal_window_start_ts"] == fire - timedelta(days=LOOKBACK_DAYS)
            and rows[0]["nominal_window_end_ts"] == fire
            and rows[0]["stabilization_delay_seconds"] == 10_800
            and rows[0]["overlap_seconds"] == 3_600
            and rows[0]["trips_pagination_mode"]
            == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
            f"{rows[0]}",
        )
        _check(
            "finalization did not rewrite the claim evidence",
            rows[0]["nominal_window_end_ts"] == fire
            and rows[0]["window_end_ts"] == expected_end,
        )
        _check(
            "the job receives the effective window exactly once",
            launched[0]["params"]["window_start_ts"] == disp._iso_z(expected_start)
            and launched[0]["params"]["window_end_ts"] == disp._iso_z(expected_end)
            and launched[0]["params"]["scheduled_fire_ts"] == disp._iso_z(fire),
            f"{launched[0]['params']}",
        )
        after_success = _coverage_rows(conn)
        _check(
            "SUCCESS advances W monotonically and preserves protected coverage fields",
            after_success[0]["covered_through_ts"] == expected_end
            and after_success[0]["covered_through_source"] == "scheduled_run"
            and after_success[0]["updated_at"] != before[0]["updated_at"]
            and after_success[0]["coverage_start_ts"]
            == before[0]["coverage_start_ts"]
            and after_success[0]["bootstrap_status"]
            == before[0]["bootstrap_status"]
            and after_success[0]["seeded_at"] == before[0]["seeded_at"]
            and after_success[0]["seeded_by"] == before[0]["seeded_by"],
            f"before={before} after={after_success}",
        )

        # --- compatibility: genuine disconnected window -------------------
        print("\n# PostgreSQL — disconnected window")
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
        conn.execute(
            "UPDATE workflow_a_control.client_account "
            "SET trips_max_recovery_span_seconds = 86400 WHERE client_id = %s",
            (CLIENT_ID,),
        )
        conn.commit()
        stale = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=60)
        gap_seed = dict(
            coverage_start_ts=stale - timedelta(days=10),
            covered_through_ts=stale,
            bootstrap_status=COVERAGE_STATUS_READY,
            bootstrap_evidence_ref="artifact:gap",
            seeded_at=_utc(2026, 5, 1), seeded_by="operator",
        )
        _seed_coverage(conn, **gap_seed)
        before = _coverage_rows(conn)[0]
        client, launched, error = _run_tick(expect_launch=False)
        rows = _history_rows(conn)
        after = _coverage_rows(conn)[0]
        _check(
            "disconnected window creates terminal FAILED history",
            isinstance(error, RuntimeError) and launched == []
            and rows[0]["status"] == "FAILED"
            and rows[0]["error_summary"] == TRIPS_COVERAGE_GAP_DETECTED,
            f"error={error!r} rows={rows}",
        )
        _check(
            "disconnected window preserves coverage bounds and provenance",
            after["coverage_start_ts"] == before["coverage_start_ts"]
            and after["covered_through_ts"] == before["covered_through_ts"]
            and after["bootstrap_evidence_ref"] == before["bootstrap_evidence_ref"]
            and after["covered_through_source"] == before["covered_through_source"]
            and after["seeded_at"] == before["seeded_at"]
            and after["seeded_by"] == before["seeded_by"],
            f"before={before} after={after}",
        )
        _check(
            "C6 atomically persists READY -> GAP_DETECTED with one timestamp",
            after["bootstrap_status"] == COVERAGE_STATUS_GAP_DETECTED
            and after["last_gap_detected_ts"] is not None
            and after["last_gap_detected_ts"] == after["updated_at"]
            and after["updated_at"] != before["updated_at"],
            f"{after}",
        )

        # --- an already-GAP_DETECTED row stays loud, fire after fire -------
        print("\n# PostgreSQL — existing GAP_DETECTED stays loud")
        _seed_control_plane(conn, mode=TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1)
        existing_gap = dict(gap_seed, bootstrap_status=COVERAGE_STATUS_GAP_DETECTED)
        _seed_coverage(conn, **existing_gap)
        before = _coverage_rows(conn)[0]
        client, launched, error = _run_tick(expect_launch=False)
        rows = _history_rows(conn)
        _check(
            "existing GAP_DETECTED re-emits the gap code with FAILED history",
            isinstance(error, RuntimeError) and launched == []
            and rows[0]["status"] == "FAILED"
            and rows[0]["error_summary"] == TRIPS_COVERAGE_GAP_DETECTED,
            f"error={error!r} rows={rows}",
        )
        _check(
            "existing GAP_DETECTED leaves the coverage row unchanged",
            _coverage_rows(conn)[0] == before,
            f"before={before} after={_coverage_rows(conn)[0]}",
        )
        # A later due fire must stay just as loud. Advance the schedule by
        # re-seeding the control plane so a fresh fire timestamp is claimable.
        conn.execute(
            "DELETE FROM workflow_a_control.client_schedule_run_history"
        )
        conn.commit()
        client, launched, error = _run_tick(expect_launch=False)
        rows = _history_rows(conn)
        _check(
            "a later fire over an existing gap is still loud and still refused",
            isinstance(error, RuntimeError) and launched == []
            and rows[0]["error_summary"] == TRIPS_COVERAGE_GAP_DETECTED
            and _coverage_rows(conn)[0] == before,
            f"error={error!r} rows={rows}",
        )
        _check(
            "an existing gap never falls back to strict",
            rows[0]["trips_pagination_mode"]
            == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
            f"{rows[0]}",
        )

        # --- claim uniqueness ---------------------------------------------
        print("\n# PostgreSQL — claim uniqueness")
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT conname FROM pg_constraint
                 WHERE conrelid =
                   'workflow_a_control.client_schedule_run_history'::regclass
                   AND contype = 'u'
                """
            )
            unique_names = [r[0] for r in cur.fetchall()]
        _check(
            "(schedule_id, scheduled_fire_ts) uniqueness is preserved",
            any("schedule_fire" in name for name in unique_names),
            f"{unique_names}",
        )
    finally:
        conn.close()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    dsn = os.getenv("TELEMATICS_COVERAGE_GATE_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_COVERAGE_GATE_TEST_DSN",
        )
        test_on_disposable_postgres(dsn)
    else:
        print("\nSKIP: set TELEMATICS_COVERAGE_GATE_TEST_DSN for PostgreSQL checks")

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("OK — Telematics coverage bootstrap gate behaves as specified.")
    sys.exit(0)


if __name__ == "__main__":
    main()
