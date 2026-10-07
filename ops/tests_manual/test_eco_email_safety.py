#!/usr/bin/env python3
"""Network/DB-free regressions for shared Eco email fail-closed policy."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from jobs.ecodriving.email_safety import *

W = ZoneInfo("Europe/Warsaw")

def expect(code, fn):
    try: fn()
    except EcoEmailPreconditionError as exc:
        assert exc.code == code, (exc.code, code)
        return exc
    raise AssertionError(f"expected {code}")

def main():
    end = datetime(2026, 7, 20, 0, 0, tzinfo=W)
    weekly = PeriodCandidate(date(2026, 7, 1), date(2026, 7, 20), "2026-07-W3")
    assert evaluate_period(weekly, report_type="weekly", clock=end).eligible
    assert evaluate_period(weekly, report_type="weekly", clock=end-timedelta(microseconds=1)).state == "open"
    assert evaluate_period(weekly, report_type="weekly", clock=end-timedelta(microseconds=1)).rejection_code == PERIOD_NOT_CLOSED
    # Partial first and final cumulative boundaries.
    assert evaluate_period(PeriodCandidate(date(2026, 7, 1), date(2026, 7, 6)), report_type="weekly", clock=end).eligible
    assert evaluate_period(PeriodCandidate(date(2026, 7, 1), date(2026, 8, 1)), report_type="weekly", clock=datetime(2026,8,1,tzinfo=W)).eligible
    invalid = evaluate_period(PeriodCandidate(date(2026,7,2), date(2026,7,6)), report_type="weekly", clock=end)
    assert invalid.rejection_code == INVALID_PERIOD_BOUNDARY
    future = evaluate_period(PeriodCandidate(date(2026,8,1), date(2026,8,3)), report_type="weekly", clock=end)
    assert future.state == "future" and future.rejection_code == PERIOD_END_IN_FUTURE
    selected = select_latest_closed_period([
        PeriodCandidate(date(2026,7,1), date(2026,7,27), "W4"), weekly,
    ], report_type="weekly", clock=datetime(2026,7,26,12,tzinfo=W))
    assert selected.selected == weekly
    assert selected.diagnostics()["rejection_reasons"] == {PERIOD_NOT_CLOSED: 1}
    early_snapshot = {"period_start_date":date(2026,7,1),"period_end_date":date(2026,7,27),"period_label":"W4","snapshot_min_updated_at":datetime(2026,7,22,tzinfo=W),"snapshot_max_updated_at":datetime(2026,7,22,tzinfo=W)}
    finalized_snapshot = {"period_start_date":date(2026,7,1),"period_end_date":date(2026,7,20),"period_label":"W3","snapshot_min_updated_at":datetime(2026,7,22,tzinfo=W),"snapshot_max_updated_at":datetime(2026,7,22,tzinfo=W)}
    snapshot_selection = select_period_for_send([early_snapshot, finalized_snapshot], report_type="weekly", contract=resolve_execution_contract({}), clock=datetime(2026,7,27,0,5,tzinfo=W))
    assert snapshot_selection.selected.period_label == "W3"
    assert snapshot_selection.diagnostics()["rejection_reasons"] == {SNAPSHOT_NOT_FINALIZED: 1}
    exc = expect(NO_ELIGIBLE_CLOSED_PERIOD, lambda: select_latest_closed_period([
        PeriodCandidate(date(2026,7,1), date(2026,7,27))
    ], report_type="weekly", clock=datetime(2026,7,26,12,tzinfo=W)))
    assert exc.diagnostics["rejected_candidate_count"] == 1
    # Leap year/month rollover and future monthly period.
    assert evaluate_period(PeriodCandidate(date(2024,2,1),date(2024,3,1)), report_type="monthly", clock=datetime(2024,3,1,tzinfo=W)).eligible
    assert evaluate_period(PeriodCandidate(date(2026,12,1),date(2027,1,1)), report_type="monthly", clock=datetime(2027,1,1,tzinfo=W)).eligible
    assert evaluate_period(PeriodCandidate(date(2026,8,1),date(2026,9,1)), report_type="monthly", clock=end).rejection_code == PERIOD_END_IN_FUTURE
    # DST boundary instants remain exact local midnights.
    assert evaluate_period(PeriodCandidate(date(2026,3,1),date(2026,4,1)), report_type="monthly", clock=datetime(2026,4,1,tzinfo=W)).eligible
    assert evaluate_period(PeriodCandidate(date(2026,10,1),date(2026,11,1)), report_type="monthly", clock=datetime(2026,11,1,tzinfo=W)).eligible
    expect(INVALID_PERIOD_BOUNDARY, lambda: evaluate_period(weekly, report_type="weekly", clock=datetime(2026,7,20)))
    # Modes and override gates.
    assert resolve_execution_contract({}).mode is ExecutionMode.RENDER_ONLY
    assert resolve_execution_contract({"dry_run": True}).mode is ExecutionMode.RENDER_ONLY
    expect(EXECUTION_MODE_REQUIRED, lambda: resolve_execution_contract({"dry_run": False}))
    expect(EXECUTION_MODE_REQUIRED, lambda: resolve_execution_contract({"mode": "normal_send"}))
    expect(INVALID_EXECUTION_MODE, lambda: resolve_execution_contract({"send_scope": "normal"}))
    expect(TEST_RECIPIENT_REQUIRED, lambda: resolve_execution_contract({"execution_mode":"test_send"}))
    test = resolve_execution_contract({"execution_mode":"test_send","test_recipient_email":"qa@example.test","allow_unclosed_period_for_test":True})
    assert require_explicit_period(weekly, report_type="weekly", contract=test, clock=end-timedelta(microseconds=1)).override_used
    normal = resolve_execution_contract({"execution_mode":"normal_send"})
    expect(PERIOD_NOT_CLOSED, lambda: require_explicit_period(weekly, report_type="weekly", contract=normal, clock=end-timedelta(microseconds=1)))
    expect(UNCLOSED_OVERRIDE_NOT_ALLOWED, lambda: resolve_execution_contract({"execution_mode":"normal_send","allow_unclosed_period_for_test":True}))
    expect(FORCE_RESEND_REASON_REQUIRED, lambda: resolve_execution_contract({"execution_mode":"force_resend"}))
    assert resolve_execution_contract({"execution_mode":"force_resend","force_resend_reason":"operator reviewed incident"}).force_resend
    print("OK - shared Eco email fail-closed safety")

if __name__ == "__main__": main()
