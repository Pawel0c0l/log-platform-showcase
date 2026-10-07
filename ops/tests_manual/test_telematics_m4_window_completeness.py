#!/usr/bin/env python3
"""M4 — the effective window was exactly tiled and every sub-window completed.

WHAT THIS PINS.
    §6 condition 6. `rc == 0` plus a verified, coverage-eligible M3 record is
    necessary and, after M4, still not sufficient: the child must also have
    proved that the sub-windows it attempted **exactly tile** `[E_start, E_end)`
    and that every one of them reached a valid complete terminal state.

    The failure class this exists for is the one M3 structurally cannot see: a
    sub-window that was NEVER ATTEMPTED. Every sub-window that *was* attempted
    passes the compatibility fetch contract's invariants perfectly, so "they all
    succeeded" says nothing about whether they covered the window. The
    deliberately sharpest case here is
    `test_a_tiling_short_by_exactly_one_otherwise_valid_subwindow_refuses`
    (docs/20 §21.8): every unit present is flawless, and it must still refuse.

WHY IT IS WRITTEN AT THIS LEVEL.
    Tiling and completeness are decided in a pure phase, before the finalizer
    opens its transaction — that is what makes a refusal cost no coverage SQL.
    So they need no database. The durable projection, the shared transaction
    boundary and the first-seen upsert semantics are genuinely about PostgreSQL
    and live in `test_telematics_m4_provider_request_log_postgres.py`.
"""
from __future__ import annotations

import dataclasses
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.tests_manual import telematics_execution_outcome_fixtures as eo_fixtures  # noqa: E402
from jobs.api.telematics import provider_client as pc  # noqa: E402
from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402
from jobs.api.telematics.execution_outcome import (  # noqa: E402
    EXECUTION_OUTCOME_VERSION,
    EXECUTION_OUTCOME_VERSION_V1,
    ExecutionOutcome,
    ExecutionOutcomeError,
)
from jobs.api.telematics.request_evidence import (  # noqa: E402
    INCOMPLETE_NOT_TERMINATED,
    INCOMPLETE_NO_SUBWINDOW,
    INCOMPLETE_SPLIT_UNVERIFIABLE,
    SUBWINDOW_COMPLETE,
    SUBWINDOW_INCOMPLETE,
    SUBWINDOW_INCOMPLETE_REFUSAL,
    TERMINATION_SHORT_PAGE,
    TOTAL_RECONCILIATION_ABSENT,
    TOTAL_RECONCILIATION_EXACT,
    WINDOW_EVIDENCE_ABSENT,
    WINDOW_EVIDENCE_IDENTITY_MISMATCH,
    WINDOW_EVIDENCE_MALFORMED,
    WINDOW_TILING_INCOMPLETE,
    RequestEvidenceCollector,
    WindowCompleteness,
    WindowCompletenessError,
    verify_window_completeness,
)

WINDOW_START = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)

_failures: List[str] = []


def _check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


def _refusal(label: str, expected_code: str, build) -> None:
    """Assert that `build()` refuses, and refuses for the stated reason."""
    try:
        build()
    except WindowCompletenessError as exc:
        _check(f"{label}: refused as {expected_code}", exc.code == expected_code)
        if exc.code != expected_code:
            print(f"      actual: {exc.code}: {exc.message}")
        return
    _check(f"{label}: refused as {expected_code}", False)


def _tiling(subwindows: int = 4, **kwargs) -> WindowCompleteness:
    return eo_fixtures.complete_window(
        window_start_ts=WINDOW_START, window_end_ts=WINDOW_END,
        subwindows=subwindows, **kwargs,
    )


def _verify(completeness: Optional[WindowCompleteness]) -> WindowCompleteness:
    return verify_window_completeness(
        completeness, window_start_ts=WINDOW_START, window_end_ts=WINDOW_END,
    )


def _replace_sub(completeness: WindowCompleteness, index: int, **changes):
    """Return `completeness` with sub-window `index` (1-based) modified."""
    subs = list(completeness.subwindows)
    subs[index - 1] = dataclasses.replace(subs[index - 1], **changes)
    return dataclasses.replace(completeness, subwindows=tuple(subs))


# ---------------------------------------------------------------------------
# 1 / 2 — a healthy run advances, including a genuine zero-row window
# ---------------------------------------------------------------------------

def test_a_complete_exact_tiling_is_accepted() -> None:
    print("\n## test_a_complete_exact_tiling_is_accepted")
    proof = _tiling(subwindows=4)
    verified = _verify(proof)
    _check("complete tiling: accepted", verified is proof)
    _check("complete tiling: all four units are COMPLETE",
           all(sub.complete for sub in verified.subwindows))
    _check("complete tiling: the units span the window exactly",
           verified.subwindows[0].covers_from_ts == WINDOW_START
           and verified.subwindows[-1].covers_to_ts == WINDOW_END)


def test_a_zero_row_window_still_advances() -> None:
    print("\n## test_a_zero_row_window_still_advances")
    # A genuine zero-row day is not an incomplete window (docs/20 §21.3). The
    # provider returned a short page — an empty one — which is the authoritative
    # terminal condition, so completeness holds and coverage must still advance.
    proof = _tiling(subwindows=2, row_count=0)
    verified = _verify(proof)
    _check("zero-row window: accepted", verified is proof)
    _check("zero-row window: every page really returned nothing",
           all(page.row_count == 0
               for sub in verified.subwindows for page in sub.pages))

    outcome = ExecutionOutcome.from_mapping(
        eo_fixtures.committed_outcome(
            {
                "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
                "client_code": "TEST00001",
                "window_start_ts": WINDOW_START.isoformat(),
                "window_end_ts": WINDOW_END.isoformat(),
            },
            schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
            platform_run_id="f6222a11-06ee-4e4f-8b25-302a9d963cfa",
            upserted=0,
            completeness=proof,
        )
    )
    _check("zero-row window: the record is EXECUTED_ZERO_ROWS_COMMITTED",
           outcome.outcome == "EXECUTED_ZERO_ROWS_COMMITTED")
    _check("zero-row window: it survives the completeness gate",
           _verify(outcome.window_completeness) is not None)


# ---------------------------------------------------------------------------
# 3 / 4 / 5 / 21 — the tiling itself
# ---------------------------------------------------------------------------

def test_a_missing_expected_subwindow_refuses() -> None:
    print("\n## test_a_missing_expected_subwindow_refuses")
    proof = _tiling(subwindows=4)
    # Drop the second unit entirely: a chunk-iteration defect that skipped one.
    without_middle = dataclasses.replace(
        proof, subwindows=(proof.subwindows[0],) + proof.subwindows[2:],
    )
    _refusal("missing middle sub-window", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(without_middle))

    _refusal(
        "missing first sub-window", WINDOW_TILING_INCOMPLETE,
        lambda: _verify(dataclasses.replace(
            proof, subwindows=proof.subwindows[1:])),
    )
    _refusal(
        "missing final sub-window", WINDOW_TILING_INCOMPLETE,
        lambda: _verify(dataclasses.replace(
            proof, subwindows=proof.subwindows[:-1])),
    )
    _refusal(
        "no sub-window at all", WINDOW_TILING_INCOMPLETE,
        lambda: _verify(dataclasses.replace(proof, subwindows=())),
    )


def test_a_tiling_short_by_exactly_one_otherwise_valid_subwindow_refuses() -> None:
    print("\n## test_a_tiling_short_by_exactly_one_otherwise_valid_subwindow_refuses")
    # The case docs/20 §21.8 adds, and the whole reason M4 exists. Every unit
    # present is flawless: it terminated on a short page, reconciled, fetched
    # its pages in order. The ONLY defect is that the set stops one unit early —
    # invisible to every per-sub-window invariant, and it must still refuse.
    proof = _tiling(subwindows=4)
    short = dataclasses.replace(proof, subwindows=proof.subwindows[:-1])
    _check("short-by-one: every remaining unit is individually flawless",
           all(sub.complete and sub.termination_reason == TERMINATION_SHORT_PAGE
               for sub in short.subwindows))
    _refusal("short-by-one tiling", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(short))


def test_a_gap_between_subwindows_refuses() -> None:
    print("\n## test_a_gap_between_subwindows_refuses")
    proof = _tiling(subwindows=3)
    # Pull the second unit's start forward in time, opening a hole behind it.
    gapped = _replace_sub(
        proof, 2,
        covers_from_ts=proof.subwindows[2 - 1].covers_from_ts + timedelta(hours=1),
    )
    _refusal("gap between units", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(gapped))


def test_overlapping_tiling_refuses() -> None:
    print("\n## test_overlapping_tiling_refuses")
    proof = _tiling(subwindows=3)
    overlapped = _replace_sub(
        proof, 2,
        covers_from_ts=proof.subwindows[1].covers_from_ts - timedelta(hours=1),
    )
    _refusal("overlapping units", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(overlapped))

    # A duplicated unit is an overlap too, and must not read as extra coverage.
    duplicated = dataclasses.replace(
        proof, subwindows=proof.subwindows + (proof.subwindows[1],),
    )
    _refusal("duplicated unit", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(duplicated))


def test_a_tiling_that_overhangs_the_window_refuses() -> None:
    print("\n## test_a_tiling_that_overhangs_the_window_refuses")
    proof = _tiling(subwindows=2)
    overhang = _replace_sub(
        proof, 2, covers_to_ts=WINDOW_END + timedelta(hours=1),
    )
    _refusal("tiling past the window end", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(overhang))


# ---------------------------------------------------------------------------
# 6 / 7 / 8 / 9 — an attempted sub-window that did not complete
# ---------------------------------------------------------------------------

def test_an_incomplete_subwindow_refuses() -> None:
    print("\n## test_an_incomplete_subwindow_refuses")
    # Provider failure, pagination invariant failure, budget exhaustion and a
    # failed advisory-total reconciliation all reach this module identically:
    # the fetch contract raised, so `complete_subwindow` was never called and
    # the unit is INCOMPLETE. The tiling is otherwise perfect.
    proof = _tiling(subwindows=3)
    for reason in (INCOMPLETE_NOT_TERMINATED, INCOMPLETE_NO_SUBWINDOW,
                   INCOMPLETE_SPLIT_UNVERIFIABLE):
        broken = _replace_sub(
            proof, 2,
            status=SUBWINDOW_INCOMPLETE,
            termination_reason=None,
            total_reconciliation=None,
            incomplete_reason=reason,
        )
        _check(f"incomplete unit ({reason}): the tiling itself is still exact",
               [s.covers_from_ts for s in broken.subwindows]
               == [s.covers_from_ts for s in proof.subwindows])
        _refusal(f"incomplete unit ({reason})", SUBWINDOW_INCOMPLETE_REFUSAL,
                 lambda b=broken: _verify(b))


def test_an_incomplete_subwindow_may_not_claim_a_valid_termination() -> None:
    print("\n## test_an_incomplete_subwindow_may_not_claim_a_valid_termination")
    proof = _tiling(subwindows=2)
    forged = _replace_sub(
        proof, 1, status=SUBWINDOW_INCOMPLETE,
        incomplete_reason=INCOMPLETE_NOT_TERMINATED,
    )
    _refusal(
        "INCOMPLETE claiming short_page", WINDOW_EVIDENCE_MALFORMED,
        lambda: WindowCompleteness.from_mapping(forged.as_dict()),
    )
    unexplained = _replace_sub(
        proof, 1, status=SUBWINDOW_INCOMPLETE, termination_reason=None,
        total_reconciliation=None, incomplete_reason=None,
    )
    _refusal(
        "INCOMPLETE with no reason", WINDOW_EVIDENCE_MALFORMED,
        lambda: WindowCompleteness.from_mapping(unexplained.as_dict()),
    )


# ---------------------------------------------------------------------------
# 10 — an absent advisory total stays permitted
# ---------------------------------------------------------------------------

def test_an_absent_advisory_total_remains_permitted() -> None:
    print("\n## test_an_absent_advisory_total_remains_permitted")
    # The accepted D5 Option B rule (docs/16 §5): absence of `meta.total` is a
    # PERMITTED state, not a failure. M4 must not quietly promote it into one.
    absent = _tiling(subwindows=2)
    _check("absent advisory total: the fixture really records 'absent'",
           all(sub.total_reconciliation == TOTAL_RECONCILIATION_ABSENT
               for sub in absent.subwindows))
    _check("absent advisory total: still advances", _verify(absent) is absent)

    exact = absent
    for index in range(1, len(absent.subwindows) + 1):
        exact = _replace_sub(
            exact, index, total_reconciliation=TOTAL_RECONCILIATION_EXACT,
        )
    _check("reconciled advisory total: still advances", _verify(exact) is exact)

    # A third state is not a thing. Only `absent` and `exact` exist, because a
    # present total that failed to reconcile raised inside the fetch contract.
    _refusal(
        "unknown reconciliation state", WINDOW_EVIDENCE_MALFORMED,
        lambda: WindowCompleteness.from_mapping(
            _replace_sub(absent, 1, total_reconciliation="approximately").as_dict()
        ),
    )


# ---------------------------------------------------------------------------
# 11 / 12 — malformed and mismatched evidence
# ---------------------------------------------------------------------------

def test_absent_evidence_refuses() -> None:
    print("\n## test_absent_evidence_refuses")
    _refusal("no completeness carrier at all", WINDOW_EVIDENCE_ABSENT,
             lambda: _verify(None))

    # A pre-M4 `/1` record is exactly this case: readable, M3-eligible, and
    # carrying no proof. It must remain incapable of advancing a watermark.
    v2 = eo_fixtures.committed_outcome(
        {
            "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
            "client_code": "TEST00001",
            "window_start_ts": WINDOW_START.isoformat(),
            "window_end_ts": WINDOW_END.isoformat(),
        },
        schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
        platform_run_id="f6222a11-06ee-4e4f-8b25-302a9d963cfa",
    )
    v1 = {k: v for k, v in v2.items() if k != "window_completeness"}
    v1["version"] = EXECUTION_OUTCOME_VERSION_V1
    legacy = ExecutionOutcome.from_mapping(v1)
    _check("a pre-M4 /1 record still parses", legacy.version == EXECUTION_OUTCOME_VERSION_V1)
    _refusal("a pre-M4 /1 record", WINDOW_EVIDENCE_ABSENT,
             lambda: _verify(legacy.window_completeness))


def test_malformed_evidence_refuses() -> None:
    print("\n## test_malformed_evidence_refuses")
    good = _tiling(subwindows=2).as_dict()

    _refusal("wrong carrier version", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(
                 dict(good, version="telematics-trips-window-completeness/9")))
    _refusal("unknown field", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(good, extra=1)))
    _refusal("missing field", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(
                 {k: v for k, v in good.items() if k != "endpoint"}))
    _refusal("not an object", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(["nope"]))

    subs = [dict(sub) for sub in good["subwindows"]]
    _refusal("naive-datetime boundary", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good, subwindows=[dict(subs[0], covers_from_ts="2026-08-10T00:00:00")]
                 + subs[1:])))
    _refusal("zero-width tiling unit", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good, subwindows=[dict(subs[0], covers_to_ts=subs[0]["covers_from_ts"])]
                 + subs[1:])))
    _refusal("COMPLETE unit with no page", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good, subwindows=[dict(subs[0], pages=[])] + subs[1:])))
    _refusal("page numbering that skips", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good,
                 subwindows=[dict(subs[0], pages=[dict(subs[0]["pages"][0], page=2)])]
                 + subs[1:])))
    _refusal("response received before the request started",
             WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good,
                 subwindows=[dict(subs[0], pages=[dict(
                     subs[0]["pages"][0],
                     response_received_at_utc="2020-01-01T00:00:00Z")])]
                 + subs[1:])))
    _refusal("non-UUID request identity", WINDOW_EVIDENCE_MALFORMED,
             lambda: WindowCompleteness.from_mapping(dict(
                 good,
                 subwindows=[dict(subs[0], pages=[dict(
                     subs[0]["pages"][0], request_id="not-a-uuid")])]
                 + subs[1:])))

    # A malformed carrier inside an execution record makes the record malformed,
    # in the record's own vocabulary rather than the carrier's.
    try:
        ExecutionOutcome.from_mapping(dict(
            eo_fixtures.committed_outcome(
                {
                    "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
                    "client_code": "TEST00001",
                    "window_start_ts": WINDOW_START.isoformat(),
                    "window_end_ts": WINDOW_END.isoformat(),
                },
                schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
                platform_run_id="f6222a11-06ee-4e4f-8b25-302a9d963cfa",
            ),
            window_completeness={"version": "wrong"},
        ))
        _check("a malformed carrier makes the whole record malformed", False)
    except ExecutionOutcomeError as exc:
        _check("a malformed carrier makes the whole record malformed",
               exc.code == "EXECUTION_OUTCOME_MALFORMED")


def test_evidence_for_another_window_refuses() -> None:
    print("\n## test_evidence_for_another_window_refuses")
    proof = _tiling(subwindows=2)

    _refusal(
        "evidence for a different window start", WINDOW_EVIDENCE_IDENTITY_MISMATCH,
        lambda: verify_window_completeness(
            proof, window_start_ts=WINDOW_START - timedelta(hours=1),
            window_end_ts=WINDOW_END),
    )
    _refusal(
        "evidence for a different window end", WINDOW_EVIDENCE_IDENTITY_MISMATCH,
        lambda: verify_window_completeness(
            proof, window_start_ts=WINDOW_START,
            window_end_ts=WINDOW_END + timedelta(hours=1)),
    )
    _refusal(
        "evidence for another endpoint", WINDOW_EVIDENCE_IDENTITY_MISMATCH,
        lambda: _verify(dataclasses.replace(proof, endpoint="/vehicles/events")),
    )
    # A tiling that internally spans a *different* window than it declares is
    # caught too — declaring the right window is not the same as covering it.
    forged = dataclasses.replace(
        proof,
        subwindows=tuple(
            dataclasses.replace(
                sub,
                covers_from_ts=sub.covers_from_ts + timedelta(days=1),
                covers_to_ts=sub.covers_to_ts + timedelta(days=1),
            )
            for sub in proof.subwindows
        ),
    )
    _refusal("a tiling that covers a shifted window", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(forged))


# ---------------------------------------------------------------------------
# The collector: what the job and the provider client actually produce
# ---------------------------------------------------------------------------

class _FakeEvidenceRun:
    """Drives the collector exactly as the job and provider client do."""

    def __init__(self, start: datetime, end: datetime) -> None:
        self.collector = RequestEvidenceCollector(
            effective_window_start_ts=start, effective_window_end_ts=end,
        )

    def unit(self, chunk, *, pages=1, rows=2, terminate=True,
             provider_subwindows=1):
        self.collector.begin_tiling_unit(
            index=chunk.index,
            covers_from=chunk.request_start_ts,
            covers_to=chunk.exclusive_end_ts,
            requested_from=chunk.request_start_ts,
            requested_to=chunk.request_end_ts,
        )
        for _ in range(provider_subwindows):
            self.collector.begin_subwindow(
                label=pc.sub_window_label(
                    chunk.request_start_ts, chunk.request_end_ts),
                requested_from=chunk.request_start_ts,
                requested_to=chunk.request_end_ts,
                wire_start="2026-08-10 00:00:00",
                wire_end="2026-08-10 23:59:59",
            )
            now = datetime.now(timezone.utc)
            for page in range(1, pages + 1):
                request_id = self.collector.record_page(
                    page=page, request_started_at_utc=now,
                    response_received_at_utc=now, http_status=200,
                    row_count=rows,
                )
                self.collector.record_first_seen(
                    request_id=request_id,
                    identities=[chunk.index * 1000 + page * 10 + n
                                for n in range(rows)],
                )
            if terminate:
                self.collector.complete_subwindow(
                    termination_reason=TERMINATION_SHORT_PAGE,
                    total_reconciliation=TOTAL_RECONCILIATION_ABSENT,
                )
        self.collector.end_tiling_unit()


def test_the_real_chunk_builder_produces_an_exact_tiling() -> None:
    print("\n## test_the_real_chunk_builder_produces_an_exact_tiling")
    # The job's own chunk builder is the tiling, so the proof is built from it
    # rather than from a hand-written fixture. If `_build_trip_fetch_chunks`
    # ever stopped tiling exactly, this fails here rather than in production.
    for chunk_days in (1, 2, 5):
        chunks = job._build_trip_fetch_chunks(
            WINDOW_START, WINDOW_END, chunk_days=chunk_days,
        )
        run = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
        for chunk in chunks:
            run.unit(chunk)
        proof = run.collector.build()
        try:
            _verify(proof)
            accepted = True
        except WindowCompletenessError as exc:
            accepted = False
            print(f"      {exc.code}: {exc.message}")
        _check(f"chunk_days={chunk_days}: the real tiling verifies "
               f"({len(chunks)} unit(s))", accepted)


def test_a_skipped_chunk_iteration_refuses() -> None:
    print("\n## test_a_skipped_chunk_iteration_refuses")
    # The defect M4 exists for, reproduced at its source: the loop skips one
    # chunk. Nothing else is wrong — every chunk it *did* run was perfect.
    chunks = job._build_trip_fetch_chunks(
        WINDOW_START, WINDOW_END, chunk_days=1,
    )
    run = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
    for chunk in chunks:
        if chunk.index == 2:
            continue
        run.unit(chunk)
    _refusal("a chunk-iteration defect", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(run.collector.build()))


def test_a_truncated_effective_window_refuses() -> None:
    print("\n## test_a_truncated_effective_window_refuses")
    # The job silently builds chunks for a shorter window than it claims: every
    # chunk runs perfectly, and the tail is simply never requested.
    chunks = job._build_trip_fetch_chunks(
        WINDOW_START, WINDOW_END - timedelta(days=1), chunk_days=1,
    )
    run = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
    for chunk in chunks:
        run.unit(chunk)
    _refusal("a silently truncated effective window", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(run.collector.build()))


def test_a_subwindow_that_never_terminated_is_incomplete() -> None:
    print("\n## test_a_subwindow_that_never_terminated_is_incomplete")
    # A provider safety stop raises out of the fetch, so `complete_subwindow`
    # is never reached for that unit. The collector must record that, not
    # assume it.
    chunks = job._build_trip_fetch_chunks(
        WINDOW_START, WINDOW_END, chunk_days=2,
    )
    run = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
    run.unit(chunks[0])
    run.unit(chunks[1], terminate=False)
    proof = run.collector.build()
    _check("aborted unit: recorded INCOMPLETE",
           proof.subwindows[1].status == SUBWINDOW_INCOMPLETE)
    _check("aborted unit: says why",
           proof.subwindows[1].incomplete_reason == INCOMPLETE_NOT_TERMINATED)
    _check("aborted unit: claims no termination",
           proof.subwindows[1].termination_reason is None)
    _refusal("a unit that never terminated", SUBWINDOW_INCOMPLETE_REFUSAL,
             lambda: _verify(proof))


def test_an_unattributable_provider_split_is_incomplete() -> None:
    print("\n## test_an_unattributable_provider_split_is_incomplete")
    # `chunk_days <= TRIPS_MAX_CHUNK_DAYS (5)` is strictly below
    # `TRIPS_MAX_SUB_WINDOW_DAYS (30)`, so the provider's inner split is 1:1 for
    # every reachable configuration. The collector REQUIRES that rather than
    # assuming it: if the split ever became real, the unit is INCOMPLETE and
    # coverage stalls loudly instead of advancing on a tiling nobody verified.
    _check("the constants really do make the inner split 1:1",
           job.TRIPS_MAX_CHUNK_DAYS < pc.TRIPS_MAX_SUB_WINDOW_DAYS)

    chunks = job._build_trip_fetch_chunks(
        WINDOW_START, WINDOW_END, chunk_days=2,
    )
    run = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
    run.unit(chunks[0], provider_subwindows=2)
    run.unit(chunks[1])
    proof = run.collector.build()
    _check("a split unit: recorded INCOMPLETE",
           proof.subwindows[0].incomplete_reason == INCOMPLETE_SPLIT_UNVERIFIABLE)
    _refusal("a unit the provider split", SUBWINDOW_INCOMPLETE_REFUSAL,
             lambda: _verify(proof))

    # A unit whose provider sub-window asked for a different interval than the
    # unit declared is the same class of unattributable evidence.
    run2 = _FakeEvidenceRun(WINDOW_START, WINDOW_END)
    run2.collector.begin_tiling_unit(
        index=1, covers_from=WINDOW_START, covers_to=WINDOW_END,
        requested_from=WINDOW_START, requested_to=WINDOW_END,
    )
    run2.collector.begin_subwindow(
        label="drifted", requested_from=WINDOW_START,
        requested_to=WINDOW_END - timedelta(days=1),
        wire_start="a", wire_end="b",
    )
    now = datetime.now(timezone.utc)
    run2.collector.record_page(
        page=1, request_started_at_utc=now, response_received_at_utc=now,
        http_status=200, row_count=1,
    )
    run2.collector.complete_subwindow(
        termination_reason=TERMINATION_SHORT_PAGE,
        total_reconciliation=TOTAL_RECONCILIATION_ABSENT,
    )
    run2.collector.end_tiling_unit()
    drifted = run2.collector.build()
    _check("a drifted provider interval: recorded INCOMPLETE",
           drifted.subwindows[0].incomplete_reason == INCOMPLETE_SPLIT_UNVERIFIABLE)


def test_first_seen_binding_is_once_only() -> None:
    print("\n## test_first_seen_binding_is_once_only")
    # A trip returned again — on a later page, a later sub-window, or an
    # overlapping tiling unit — keeps its FIRST sighting. This is the property
    # that makes a repeated observation unable to create a second first-seen
    # event, and it is enforced here as well as by the SQL's omission of the
    # column from `DO UPDATE SET`.
    collector = RequestEvidenceCollector(
        effective_window_start_ts=WINDOW_START,
        effective_window_end_ts=WINDOW_END,
    )
    collector.begin_tiling_unit(
        index=1, covers_from=WINDOW_START, covers_to=WINDOW_END,
        requested_from=WINDOW_START, requested_to=WINDOW_END,
    )
    collector.begin_subwindow(
        label="sw", requested_from=WINDOW_START, requested_to=WINDOW_END,
        wire_start="a", wire_end="b",
    )
    now = datetime.now(timezone.utc)
    first = collector.record_page(
        page=1, request_started_at_utc=now, response_received_at_utc=now,
        http_status=200, row_count=2,
    )
    collector.record_first_seen(request_id=first, identities=[10, 11])
    second = collector.record_page(
        page=2, request_started_at_utc=now, response_received_at_utc=now,
        http_status=200, row_count=2,
    )
    collector.record_first_seen(request_id=second, identities=[11, 12])

    bindings = collector.first_seen_request_ids()
    _check("first-seen: a trip seen once binds to that request",
           bindings[10] == first)
    _check("first-seen: a re-observed trip keeps its FIRST request",
           bindings[11] == first)
    _check("first-seen: a newly seen trip binds to the request that found it",
           bindings[12] == second)
    _check("first-seen: the two requests really are different identities",
           first != second)
    _check("first-seen: every binding is a canonical UUID",
           all(str(uuid.UUID(value)) == value for value in bindings.values()))


def test_evidence_recorded_outside_a_tiling_unit_is_dropped() -> None:
    print("\n## test_evidence_recorded_outside_a_tiling_unit_is_dropped")
    # A sub-window fetched outside any declared unit cannot be attributed, so it
    # is dropped rather than guessed at — and the unit it should have belonged
    # to is then simply missing from the tiling, which refuses.
    collector = RequestEvidenceCollector(
        effective_window_start_ts=WINDOW_START,
        effective_window_end_ts=WINDOW_END,
    )
    collector.begin_subwindow(
        label="orphan", requested_from=WINDOW_START, requested_to=WINDOW_END,
        wire_start="a", wire_end="b",
    )
    now = datetime.now(timezone.utc)
    orphan_id = collector.record_page(
        page=1, request_started_at_utc=now, response_received_at_utc=now,
        http_status=200, row_count=1,
    )
    _check("an orphan page mints no request identity", orphan_id is None)
    _check("an orphan page binds no first-seen provenance",
           collector.first_seen_request_ids() == {})
    _refusal("a collector that saw only orphans", WINDOW_TILING_INCOMPLETE,
             lambda: _verify(collector.build()))


# ---------------------------------------------------------------------------
# Contract drift
# ---------------------------------------------------------------------------

def test_the_vocabulary_matches_the_provider_client() -> None:
    print("\n## test_the_vocabulary_matches_the_provider_client")
    # `request_evidence` holds these as literals rather than importing
    # `provider_client`, which would drag `requests` and the whole HTTP client
    # into the dispatcher's gate. That is only safe while the two agree.
    _check("'absent' matches the provider client",
           TOTAL_RECONCILIATION_ABSENT == pc.COMPAT_TOTAL_STATE_ABSENT)
    _check("'exact' matches the provider client",
           TOTAL_RECONCILIATION_EXACT == pc.COMPAT_TOTAL_RECONCILIATION_EXACT)
    _check("the writer emits the current record version",
           EXECUTION_OUTCOME_VERSION == "telematics-trips-execution-outcome/2")
    _check("a COMPLETE unit's termination is the contract's terminal condition",
           TERMINATION_SHORT_PAGE == "short_page")
    _check("SUBWINDOW_COMPLETE and SUBWINDOW_INCOMPLETE are distinct",
           SUBWINDOW_COMPLETE != SUBWINDOW_INCOMPLETE)


def test_a_skipped_record_may_not_carry_completeness() -> None:
    print("\n## test_a_skipped_record_may_not_carry_completeness")
    # A run that skipped never entered the provider, so it cannot have covered
    # anything. A skip carrying a tiling proof is the same self-contradiction as
    # a skip claiming a committed transaction.
    skipped = eo_fixtures.skipped_disabled_schedule_outcome(
        {
            "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
            "client_code": "TEST00001",
            "window_start_ts": WINDOW_START.isoformat(),
            "window_end_ts": WINDOW_END.isoformat(),
        },
        schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    )
    _check("a real skip carries no completeness",
           skipped["window_completeness"] is None)
    try:
        ExecutionOutcome.from_mapping(
            dict(skipped, window_completeness=_tiling(1).as_dict())
        )
        _check("a skip claiming completeness is refused", False)
    except ExecutionOutcomeError as exc:
        _check("a skip claiming completeness is refused",
               exc.code == "EXECUTION_OUTCOME_MALFORMED")


def main() -> int:
    test_a_complete_exact_tiling_is_accepted()
    test_a_zero_row_window_still_advances()
    test_a_missing_expected_subwindow_refuses()
    test_a_tiling_short_by_exactly_one_otherwise_valid_subwindow_refuses()
    test_a_gap_between_subwindows_refuses()
    test_overlapping_tiling_refuses()
    test_a_tiling_that_overhangs_the_window_refuses()
    test_an_incomplete_subwindow_refuses()
    test_an_incomplete_subwindow_may_not_claim_a_valid_termination()
    test_an_absent_advisory_total_remains_permitted()
    test_absent_evidence_refuses()
    test_malformed_evidence_refuses()
    test_evidence_for_another_window_refuses()
    test_the_real_chunk_builder_produces_an_exact_tiling()
    test_a_skipped_chunk_iteration_refuses()
    test_a_truncated_effective_window_refuses()
    test_a_subwindow_that_never_terminated_is_incomplete()
    test_an_unattributable_provider_split_is_incomplete()
    test_first_seen_binding_is_once_only()
    test_evidence_recorded_outside_a_tiling_unit_is_dropped()
    test_the_vocabulary_matches_the_provider_client()
    test_a_skipped_record_may_not_carry_completeness()

    print()
    if _failures:
        print(f"FAILED — {len(_failures)} assertion(s)")
        for label in _failures:
            print(f"  - {label}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
