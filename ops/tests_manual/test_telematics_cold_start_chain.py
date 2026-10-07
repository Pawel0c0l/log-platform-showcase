#!/usr/bin/env python3
"""Pure tests for the multi-window cold-start chain helper.

No database, no network, no subprocess: `ops/telematics_cold_start_chain.py` is
deliberately pure, and this suite is the proof that the two callers can rely on
one shared judgement about what a chain is.

    .venv/bin/python ops/tests_manual/test_telematics_cold_start_chain.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops import telematics_cold_start_chain as chain  # noqa: E402

UTC = timezone.utc
DAY = 86400
R31 = 2678400  # 31 days — the migration-058 ceiling and the SWEE client value

BASELINE = datetime(2026, 7, 1, tzinfo=UTC)

CID = "598bc0e3-99b6-4e38-88ea-c1c63e519b2f"
SID = "60c80b85-f294-4a00-8e09-b6a3688af443"
DATASET = "trips_sync"
CHAIN = "TELEMATICS-COLD-START-ECHO00001-2026-08"

PASSED = []


def check(name, fn) -> None:
    fn()
    PASSED.append(name)


def expect_error(code, fn) -> None:
    try:
        fn()
    except chain.ChainContractError as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
        return
    raise AssertionError(f"expected {code}")


# ---------------------------------------------------------------------------
# 1. Chain identity
# ---------------------------------------------------------------------------

def test_chain_ref_validation() -> None:
    assert chain.validate_chain_ref(CHAIN) == CHAIN
    assert chain.validate_chain_ref("  " + CHAIN + " ") == CHAIN
    for bad in ("", "  ", "-leading", "has space", "a" * 181, "x" * 200):
        expect_error("CHAIN_REF_INVALID", lambda b=bad: chain.validate_chain_ref(b))
    # A chain reference may never itself look like a window of another chain.
    for bad in ("X-W01", "X-W99", "TELEMATICS-W100"):
        expect_error("CHAIN_REF_INVALID", lambda b=bad: chain.validate_chain_ref(b))


def test_window_approval_ref_composition() -> None:
    assert chain.window_approval_ref(CHAIN, 1) == CHAIN + "-W01"
    assert chain.window_approval_ref(CHAIN, 9) == CHAIN + "-W09"
    assert chain.window_approval_ref(CHAIN, 12) == CHAIN + "-W12"
    assert chain.window_approval_ref(CHAIN, 100) == CHAIN + "-W100"
    for bad in (0, -1, 1000):
        expect_error(
            "CHAIN_ORDINAL_INVALID",
            lambda b=bad: chain.window_approval_ref(CHAIN, b),
        )
    expect_error(
        "CHAIN_ORDINAL_INVALID", lambda: chain.window_approval_ref(CHAIN, True),
    )
    # The longest accepted chain reference and the longest ordinal still compose
    # inside migration 058's 200-character `approval_ref` bound, so a valid chain
    # reference can never produce an approval reference the database rejects.
    longest = chain.window_approval_ref("A" * chain.MAX_CHAIN_REF_CHARS, 999)
    assert len(longest) <= chain.MAX_APPROVAL_REF_CHARS


def test_window_approval_ref_parsing_round_trips() -> None:
    assert chain.parse_window_approval_ref(CHAIN + "-W01") == (CHAIN, 1)
    assert chain.parse_window_approval_ref(CHAIN + "-W100") == (CHAIN, 100)
    # Not a window at all.
    for foreign in (
        "", "T-0", CHAIN, "TELEMATICS-C11-ECHO00001-COLD-START-1",
        CHAIN + "-W1",        # one digit is not the canonical rendering
        CHAIN + "-W007",      # neither is an over-padded ordinal
        CHAIN + "-W00",       # ordinal zero
        CHAIN + "-W01-W02",   # the inner segment makes the chain invalid
        CHAIN + "-w01",       # lowercase marker
    ):
        assert chain.parse_window_approval_ref(foreign) is None, foreign


def test_chain_membership_is_exact() -> None:
    assert chain.belongs_to_chain(CHAIN + "-W01", CHAIN) is True
    assert chain.belongs_to_chain(CHAIN + "-W02", CHAIN) is True
    # A different chain that shares a prefix is still a different chain.
    assert chain.belongs_to_chain(CHAIN + "-EXTRA-W01", CHAIN) is False
    assert chain.belongs_to_chain("OTHER-CHAIN-W01", CHAIN) is False
    assert chain.belongs_to_chain("T-0", CHAIN) is False


# ---------------------------------------------------------------------------
# 2. Deterministic window planning
# ---------------------------------------------------------------------------

def _plan(start, end, span):
    return chain.plan_recovery_windows(
        start=start, final_end=end, max_span_seconds=span,
    )


def _assert_plan_invariants(windows, *, start, final_end, span) -> None:
    assert windows, "a plan is never empty"
    assert windows[0][0] == start, "the first window starts at the range start"
    assert windows[-1][1] == final_end, "the last window ends exactly at the bound"
    for index, (w_start, w_end) in enumerate(windows):
        assert w_end > w_start, "every window advances"
        assert int((w_end - w_start).total_seconds()) <= span, "every window fits R"
        if index:
            previous_end = windows[index - 1][1]
            assert w_start == previous_end, "no gap and no overlap"


def test_one_window_plan() -> None:
    windows = _plan(BASELINE, BASELINE + timedelta(days=3), R31)
    assert len(windows) == 1
    _assert_plan_invariants(
        windows, start=BASELINE, final_end=BASELINE + timedelta(days=3), span=R31,
    )


def test_exact_r_plan_is_one_window() -> None:
    end = BASELINE + timedelta(seconds=R31)
    windows = _plan(BASELINE, end, R31)
    assert len(windows) == 1, "an exactly-R range is one window, never two"
    assert windows[0] == (BASELINE, end)


def test_two_window_plan_matches_the_swee_shape() -> None:
    """The shape the first production consumer needs, derived — never hard-coded."""
    final = datetime(2026, 8, 4, 6, 0, tzinfo=UTC)
    windows = _plan(BASELINE, final, R31)
    assert len(windows) == 2
    assert windows[0] == (BASELINE, datetime(2026, 8, 1, tzinfo=UTC))
    assert windows[1] == (datetime(2026, 8, 1, tzinfo=UTC), final)
    _assert_plan_invariants(windows, start=BASELINE, final_end=final, span=R31)


def test_three_window_plan() -> None:
    final = BASELINE + timedelta(seconds=2 * R31 + 5 * DAY)
    windows = _plan(BASELINE, final, R31)
    assert len(windows) == 3
    _assert_plan_invariants(windows, start=BASELINE, final_end=final, span=R31)
    assert int((windows[0][1] - windows[0][0]).total_seconds()) == R31
    assert int((windows[1][1] - windows[1][0]).total_seconds()) == R31
    assert int((windows[2][1] - windows[2][0]).total_seconds()) == 5 * DAY


def test_final_short_window_is_never_widened() -> None:
    final = BASELINE + timedelta(seconds=R31 + 1)
    windows = _plan(BASELINE, final, R31)
    assert len(windows) == 2
    assert int((windows[1][1] - windows[1][0]).total_seconds()) == 1
    assert windows[-1][1] == final, "the approved boundary is exact"


def test_no_gaps_or_overlaps_across_many_shapes() -> None:
    for span in (60, 3600, DAY, R31):
        for seconds in (1, 59, 60, 61, 3599, 3600, 3601, DAY, 5 * DAY, R31, R31 + 7):
            if seconds > chain.MAX_WINDOW_ORDINAL * span:
                continue  # refused by the ordinal ceiling; covered separately
            final = BASELINE + timedelta(seconds=seconds)
            windows = _plan(BASELINE, final, span)
            _assert_plan_invariants(
                windows, start=BASELINE, final_end=final, span=span,
            )
            covered = sum(
                int((end - start).total_seconds()) for start, end in windows
            )
            assert covered == seconds, "the split is total and non-overlapping"


def test_plan_serialization_is_stable_and_reviewable() -> None:
    final = BASELINE + timedelta(seconds=R31 + DAY)
    rendered = chain.serialize_window_plan(
        _plan(BASELINE, final, R31), chain_ref=CHAIN,
    )
    assert rendered == [
        {
            "window_ordinal": 1,
            "window_start_ts": "2026-07-01T00:00:00Z",
            "window_end_ts": "2026-08-01T00:00:00Z",
            "window_span_seconds": R31,
            "approval_ref": CHAIN + "-W01",
        },
        {
            "window_ordinal": 2,
            "window_start_ts": "2026-08-01T00:00:00Z",
            "window_end_ts": "2026-08-02T00:00:00Z",
            "window_span_seconds": DAY,
            "approval_ref": CHAIN + "-W02",
        },
    ]
    # Remaining-plan rendering keeps the true ordinals.
    offset = chain.serialize_window_plan(
        _plan(BASELINE, final, R31), chain_ref=CHAIN, start_ordinal=3,
    )
    assert [item["window_ordinal"] for item in offset] == [3, 4]
    assert offset[0]["approval_ref"] == CHAIN + "-W03"


def test_invalid_bounds_are_refused() -> None:
    expect_error("PLAN_BOUND_INVALID", lambda: _plan(BASELINE, BASELINE, R31))
    expect_error(
        "PLAN_BOUND_INVALID",
        lambda: _plan(BASELINE, BASELINE - timedelta(seconds=1), R31),
    )
    expect_error("PLAN_SPAN_INVALID", lambda: _plan(BASELINE, BASELINE + timedelta(days=1), 0))
    expect_error("PLAN_SPAN_INVALID", lambda: _plan(BASELINE, BASELINE + timedelta(days=1), -1))
    expect_error(
        "PLAN_SPAN_INVALID",
        lambda: _plan(BASELINE, BASELINE + timedelta(days=1), 1.5),
    )
    # Naive and sub-second bounds are refused rather than normalized.
    expect_error(
        "PLAN_BOUND_INVALID",
        lambda: _plan(datetime(2026, 7, 1), BASELINE + timedelta(days=1), R31),
    )
    expect_error(
        "PLAN_BOUND_INVALID",
        lambda: _plan(
            BASELINE.replace(microsecond=1), BASELINE + timedelta(days=1), R31,
        ),
    )
    expect_error(
        "PLAN_TOO_MANY_WINDOWS",
        lambda: _plan(BASELINE, BASELINE + timedelta(seconds=1001), 1),
    )


# ---------------------------------------------------------------------------
# 3. Chain state evaluation
# ---------------------------------------------------------------------------

def _row(ordinal, start, end, *, status="SUCCESS", run_id=None, **overrides):
    row = {
        "recovery_run_id": f"1111{ordinal:04d}-0000-4000-8000-000000000000",
        "client_id": CID,
        "schedule_id": SID,
        "dataset_name": DATASET,
        "status": status,
        "approval_ref": chain.window_approval_ref(CHAIN, ordinal),
        "window_start_ts": start,
        "window_end_ts": end,
        "platform_run_id": run_id or f"2222{ordinal:04d}-0000-4000-8000-000000000000",
    }
    row.update(overrides)
    return row


def _two_window_rows():
    mid = BASELINE + timedelta(seconds=R31)
    end = mid + timedelta(days=3)
    return [_row(1, BASELINE, mid), _row(2, mid, end)], end


def _evaluate(rows, current_w):
    return chain.evaluate_chain(
        rows,
        baseline_ts=BASELINE,
        current_covered_through_ts=current_w,
        client_id=CID,
        schedule_id=SID,
        dataset_name=DATASET,
    )


def test_empty_chain_describes_the_first_window() -> None:
    summary = _evaluate([], BASELINE)
    assert summary["successful_window_count"] == 0
    assert summary["next_window_ordinal"] == 1
    assert summary["next_window_start_ts"] == "2026-07-01T00:00:00Z"


def test_complete_two_window_chain_is_accepted() -> None:
    rows, end = _two_window_rows()
    summary = _evaluate(rows, end)
    assert summary["successful_window_count"] == 2
    assert summary["next_window_ordinal"] == 3
    assert [w["window_ordinal"] for w in summary["windows"]] == [1, 2]
    # Unordered input is ordered by ordinal, not by insertion order.
    assert _evaluate(list(reversed(rows)), end) == summary


def test_partition_separates_foreign_rows() -> None:
    rows, _end = _two_window_rows()
    foreign = dict(rows[0])
    foreign["approval_ref"] = "TELEMATICS-C11-ECHO00001-1"
    parts = chain.partition_recovery_rows(rows + [foreign], chain_ref=CHAIN)
    assert len(parts["chain"]) == 2
    assert len(parts["foreign"]) == 1


def test_chain_refuses_every_broken_shape() -> None:
    rows, end = _two_window_rows()
    mid = BASELINE + timedelta(seconds=R31)

    # A row belonging to another client, schedule or dataset.
    for field, value in (
        ("client_id", "db8055e0-e030-4d5a-816b-ec4dc338d698"),
        ("schedule_id", "db8055e0-e030-4d5a-816b-ec4dc338d698"),
        ("dataset_name", "eco_driving_aggregate"),
    ):
        broken = [dict(rows[0]), dict(rows[1])]
        broken[1][field] = value
        expect_error(
            "CHAIN_IDENTITY_MISMATCH", lambda b=broken: _evaluate(b, end),
        )

    # Still in flight.
    for status in ("PLANNED", "RUNNING"):
        broken = [rows[0], _row(2, mid, end, status=status)]
        expect_error("CHAIN_WINDOW_ACTIVE", lambda b=broken: _evaluate(b, end))

    # A failed window blocks the chain and is never skipped.
    for status in ("FAILED", "FINALIZATION_CONFLICT"):
        broken = [_row(1, BASELINE, mid, status=status), rows[1]]
        expect_error("CHAIN_WINDOW_NOT_SUCCESS", lambda b=broken: _evaluate(b, end))

    # Ordinals must be exactly 1..N.
    expect_error(
        "CHAIN_ORDINAL_SEQUENCE_INVALID",
        lambda: _evaluate([_row(2, BASELINE, mid), _row(3, mid, end)], end),
    )
    expect_error(
        "CHAIN_ORDINAL_SEQUENCE_INVALID",
        lambda: _evaluate([rows[0], _row(3, mid, end)], end),
    )

    # Window 1 must start at the original baseline A.
    expect_error(
        "CHAIN_GEOMETRY_INVALID",
        lambda: _evaluate(
            [
                _row(1, BASELINE + timedelta(seconds=1), mid),
                _row(2, mid, end),
            ],
            end,
        ),
    )
    # A gap between windows.
    expect_error(
        "CHAIN_GEOMETRY_INVALID",
        lambda: _evaluate(
            [rows[0], _row(2, mid + timedelta(seconds=1), end)], end,
        ),
    )
    # An overlap between windows.
    expect_error(
        "CHAIN_GEOMETRY_INVALID",
        lambda: _evaluate(
            [rows[0], _row(2, mid - timedelta(seconds=1), end)], end,
        ),
    )
    # A non-advancing window.
    expect_error(
        "CHAIN_GEOMETRY_INVALID",
        lambda: _evaluate([_row(1, BASELINE, BASELINE)], BASELINE),
    )
    # The chain must end exactly at the current watermark.
    expect_error(
        "CHAIN_GEOMETRY_INVALID",
        lambda: _evaluate(rows, end + timedelta(seconds=1)),
    )


def test_business_run_correspondence_is_one_to_one() -> None:
    rows, end = _two_window_rows()
    summary = _evaluate(rows, end)
    runs = [
        {"run_id": w["platform_run_id"], "status": "SUCCESS"}
        for w in summary["windows"]
    ]
    assert chain.evaluate_business_run_correspondence(
        summary, target_runs=runs,
    ) == {
        "chain_business_runs": 2,
        "target_business_runs": 2,
        "unrelated_business_runs": 0,
    }

    # A window whose run is absent.
    expect_error(
        "CHAIN_RUN_MISSING",
        lambda: chain.evaluate_business_run_correspondence(
            summary, target_runs=runs[:1],
        ),
    )
    # A window that recorded no run at all.
    mid = BASELINE + timedelta(seconds=R31)
    no_run = _evaluate(
        [_row(1, BASELINE, mid, run_id=None), rows[1]], end,
    )
    no_run["windows"][0]["platform_run_id"] = None
    expect_error(
        "CHAIN_RUN_MISSING",
        lambda: chain.evaluate_business_run_correspondence(
            no_run, target_runs=runs,
        ),
    )
    # Two windows sharing one run.
    shared = _evaluate(
        [
            _row(1, BASELINE, mid, run_id=rows[1]["platform_run_id"]),
            rows[1],
        ],
        end,
    )
    expect_error(
        "CHAIN_RUN_DUPLICATE",
        lambda: chain.evaluate_business_run_correspondence(
            shared, target_runs=runs,
        ),
    )
    # A prior run that did not succeed.
    expect_error(
        "CHAIN_RUN_NOT_SUCCESS",
        lambda: chain.evaluate_business_run_correspondence(
            summary,
            target_runs=[
                {"run_id": runs[0]["run_id"], "status": "FAILED"}, runs[1],
            ],
        ),
    )
    # An unrelated target run.
    expect_error(
        "CHAIN_RUN_UNRELATED",
        lambda: chain.evaluate_business_run_correspondence(
            summary,
            target_runs=runs + [
                {
                    "run_id": "58c6766d-61bd-412a-80d4-95f1ff5cb17b",
                    "status": "SUCCESS",
                },
            ],
        ),
    )


def test_module_is_pure() -> None:
    text = (ROOT / "ops" / "telematics_cold_start_chain.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import psycopg", "import subprocess", "import requests", "import os",
        "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "cur.execute", "os.environ",
    ):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------

def main() -> None:
    for name, fn in sorted(
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ):
        check(name, fn)
    print(f"OK - {len(PASSED)} Telematics cold-start chain checks passed")


if __name__ == "__main__":
    main()
