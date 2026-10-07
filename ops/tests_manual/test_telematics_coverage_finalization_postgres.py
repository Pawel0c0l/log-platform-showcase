#!/usr/bin/env python3
"""C6 advancement, no-op, CAS, claim-loss and gap tests on disposable PG16."""
from __future__ import annotations
import os, sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobs.api.telematics import dispatcher as d
from jobs.api.telematics.coverage_windows import (
    COVERAGE_GATE_ALLOWED, COVERAGE_GATE_GAP_DETECTED, COVERAGE_STATUS_READY,
    TRIPS_COVERAGE_GAP_DETECTED, CoverageGateResult,
)
from jobs.trips_pagination_mode import TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 as MODE
from jobs.api.telematics.request_evidence import persist_pending_request_facts
from ops.tests_manual import telematics_execution_outcome_fixtures as eo_fixtures
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit

ENV = "TELEMATICS_C6_FINALIZATION_TEST_DSN"
MIGRATIONS = (
    "008_workflow_a_control_plane.sql", "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql", "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
    # M4: the finalizer now projects durable evidence inside this same
    # transaction, so the relation has to exist for any of these to run.
    "061_workflow_a_provider_request_log.sql",
)
CID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
SID = "7cac378a-5787-4d62-85d1-282bed208c8c"
CODE = "TST00001"
F = datetime(2026, 8, 2, tzinfo=timezone.utc)
WS, WE = F - timedelta(days=7), F - timedelta(hours=3)
A, W = datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 8, 1, tzinfo=timezone.utc)
SEEDED = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
OLD = datetime(2026, 7, 1, 13, tzinfo=timezone.utc)

def bootstrap(c):
    c.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in MIGRATIONS:
        c.execute((ROOT / "db/migrations" / name).read_text())
    c.execute("""INSERT INTO workflow_a_control.client_account
      (client_id,client_code,client_name,provider_type,provider_base_url,
       provider_basic_auth_username,provider_basic_auth_password_secret_ref,
       client_db_host,client_db_port,client_db_name,client_db_user,
       client_db_password_secret_ref,client_db_schema,speed_trigger_filter_text,
       enabled,trips_pagination_mode)
      VALUES (%s,%s,'Test','telematics','https://example.invalid','u','REF',
              '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s)""",
      (CID, CODE, MODE))
    c.execute("""INSERT INTO workflow_a_control.client_dataset_schedule
      (schedule_id,client_id,client_code,dataset_name,enabled,frequency,run_time,
       timezone,lookback_days,overwrite_existing)
      VALUES (%s,%s,%s,'trips_sync',true,'daily','02:00','UTC',7,true)""",
      (SID, CID, CODE))
    c.commit()

def schedule():
    return d.ScheduleRow(SID,CID,CODE,"Test","trips_sync",
      "jobs.api.telematics.sync_trips_and_speeding",True,"daily",None,None,False,
      time(2),"UTC",7,True,"enabled",MODE)

def gate(allowed=True):
    return CoverageGateResult(
      allowed, COVERAGE_GATE_ALLOWED if allowed else COVERAGE_GATE_GAP_DETECTED,
      None if allowed else TRIPS_COVERAGE_GAP_DETECTED, "fixture", None,
      not allowed, A, W, COVERAGE_STATUS_READY)

def seed(c, *, w=W, source="bootstrap", gap=None, hstatus="RUNNING"):
    c.execute("DELETE FROM workflow_a_control.provider_request_log")
    c.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
    c.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    c.execute("""INSERT INTO workflow_a_control.client_dataset_coverage
      (schedule_id,client_id,client_code,dataset_name,coverage_start_ts,
       covered_through_ts,bootstrap_status,bootstrap_evidence_ref,seeded_at,
       seeded_by,covered_through_source,last_gap_detected_ts,updated_at)
      VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY','artifact:safe',%s,
              'operator',%s,%s,%s)""",
      (SID,CID,CODE,A,w,SEEDED,source,gap,OLD))
    hid = c.execute("""INSERT INTO workflow_a_control.client_schedule_run_history
      (schedule_id,client_id,client_code,dataset_name,window_start_ts,window_end_ts,
       scheduled_fire_ts,status,started_at,nominal_window_start_ts,
       nominal_window_end_ts,stabilization_delay_seconds,overlap_seconds,
       trips_pagination_mode)
      VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,%s,%s,%s,%s,10800,3600,%s)
      RETURNING run_history_id::text""",
      (SID,CID,CODE,WS,WE,F,hstatus,F,F-timedelta(days=7),F,MODE)).fetchone()[0]
    c.commit()
    state = d._load_coverage_state(c, client_id=CID, dataset_name="trips_sync")
    c.rollback()
    p = d.PreparedDispatcherRun(c,schedule(),F,WS,WE,hid,[],720,False,
      F-timedelta(days=7),F,state,gate())
    return state,p

PRID = "44444444-4444-4444-8444-444444444444"


def fin(c, p, **kw):
    """Call the finalizer with the M4 arguments it now requires.

    Every one of these cases is about coverage — the CAS, the no-op, claim
    loss — so each supplies a healthy completeness proof for exactly the claimed
    window and lets the finalizer project it. That is deliberately not a
    weakening: it means each case additionally proves that the durable evidence
    and the watermark move (or fail to move) together, which is the M4
    invariant those very paths have to preserve.
    """
    kw.setdefault(
        "completeness",
        eo_fixtures.complete_window(window_start_ts=WS, window_end_ts=WE),
    )
    kw.setdefault("platform_run_id", PRID)
    # M4 step 1: the child made its request facts durable before its business
    # transaction committed. Finalization PROMOTES those rows; it does not
    # insert. Doing this here keeps every case below exercising the real
    # lifecycle rather than a shape the job never produces.
    persist_pending_request_facts(
        c, platform_run_id=kw["platform_run_id"], completeness=kw["completeness"],
    )
    c.commit()
    return d._finalize_compat_success(c, prepared=p, **kw)


def evidence_rows(c):
    from psycopg.rows import dict_row
    with c.cursor(row_factory=dict_row) as q:
        q.execute(
            "SELECT * FROM workflow_a_control.provider_request_log "
            "ORDER BY sub_window_index, page"
        )
        found = [dict(row) for row in q.fetchall()]
    c.rollback()
    return found


def rows(c):
    from psycopg.rows import dict_row
    with c.cursor(row_factory=dict_row) as q:
        q.execute("SELECT * FROM workflow_a_control.client_dataset_coverage WHERE schedule_id=%s",(SID,))
        cov=dict(q.fetchone())
        q.execute("SELECT * FROM workflow_a_control.client_schedule_run_history ORDER BY created_at DESC LIMIT 1")
        hist=dict(q.fetchone())
    c.rollback()
    return cov,hist

def test_success(c):
    state,p=seed(c)
    assert tuple(state.__dataclass_fields__) == (
      "schedule_id","client_id","client_code","dataset_name","coverage_start_ts",
      "covered_through_ts","bootstrap_status","bootstrap_evidence_ref","seeded_at",
      "seeded_by","covered_through_source","last_gap_detected_ts")
    assert p.coverage_state is state
    assert fin(c,p) is True
    cov,h=rows(c)
    assert (cov["covered_through_ts"],cov["covered_through_source"]) == (WE,"scheduled_run")
    assert cov["updated_at"] != OLD and cov["coverage_start_ts"] == A
    assert h["status"] == "SUCCESS"
    # M4: the watermark committed, so its supporting evidence committed with it,
    # stamped from the dispatcher's own claim rather than from the payload.
    ev = evidence_rows(c)
    assert len(ev) == 1
    assert str(ev[0]["run_history_id"]) == p.run_history_id
    assert str(ev[0]["platform_run_id"]) == PRID
    assert (str(ev[0]["client_id"]), str(ev[0]["schedule_id"])) == (CID, SID)
    assert ev[0]["dataset_name"] == "trips_sync" and ev[0]["endpoint"] == "/trips"
    assert (ev[0]["covers_from_ts"], ev[0]["covers_to_ts"]) == (WS, WE)
    assert ev[0]["subwindow_complete"] is True
    assert ev[0]["termination_reason"] == "short_page"
    for current in (WE,WE+timedelta(hours=1)):
        _,p=seed(c,w=current,source="operator")
        assert fin(c,p) is False
        cov,h=rows(c)
        assert (cov["covered_through_ts"],cov["covered_through_source"],cov["updated_at"]) == (current,"operator",OLD)
        assert h["status"]=="SUCCESS"
        # A validated no-op still records the requests it genuinely made: the
        # window was covered, the watermark simply had nowhere to move.
        assert len(evidence_rows(c)) == 1
    state,p=seed(c)
    harmless=OLD+timedelta(days=1)
    c.execute("""UPDATE workflow_a_control.client_dataset_coverage
      SET client_code=NULL, updated_at=%s WHERE schedule_id=%s""",(harmless,SID))
    c.commit()
    assert d._claim_coverage_params(state)["claim_covered_through_ts"]==W
    assert fin(c,p) is True
    cov,_=rows(c); assert cov["client_code"] is None

def test_cas(c,dsn):
    import psycopg
    old=d._platform_pg_conn; d._platform_pg_conn=lambda: psycopg.connect(dsn)
    try:
      changes=(("coverage_start_ts",A-timedelta(seconds=1)),
        ("covered_through_ts",W+timedelta(seconds=1)),
        ("bootstrap_status","RESEED_REQUIRED"),
        ("bootstrap_evidence_ref","artifact:other"),
        ("seeded_at",SEEDED+timedelta(seconds=1)),("seeded_by","other"),
        ("covered_through_source","operator"),("last_gap_detected_ts",F))
      for col,value in changes:
        _,p=seed(c)
        c.execute(f"UPDATE workflow_a_control.client_dataset_coverage SET {col}=%s WHERE schedule_id=%s",(value,SID)); c.commit()
        try: fin(c,p); assert False,col
        except d.CoverageFinalizationError as e: assert e.code==d.TRIPS_COVERAGE_ADVANCE_CONFLICT
        cov,h=rows(c); assert cov[col]==value and h["status"]=="FAILED"
        # M4: the CAS refused and the PROMOTION rolled back with it, so nothing
        # became coverage evidence. The request facts deliberately survive —
        # that is what keeps a committed first_seen_request_id resolvable — and
        # they stay PENDING, so they authorize nothing.
        surviving = evidence_rows(c)
        assert surviving and all(r["status"] == "PENDING" for r in surviving), surviving
        assert all(r["run_history_id"] is None and r["subwindow_complete"] is None
                   for r in surviving), surviving
      state,_=seed(c)
      row={"schedule_id":SID,"client_id":CID,"dataset_name":"trips_sync",
        "coverage_start_ts":A.astimezone(timezone(timedelta(hours=2))),
        "covered_through_ts":W.astimezone(timezone(timedelta(hours=-4))),
        "bootstrap_status":"READY","bootstrap_evidence_ref":"artifact:safe",
        "seeded_at":SEEDED.astimezone(timezone(timedelta(hours=1))),
        "seeded_by":"operator","covered_through_source":"bootstrap",
        "last_gap_detected_ts":None}
      assert d._coverage_row_matches_claim(row,state)
      row["seeded_at"]+=timedelta(microseconds=1)
      assert not d._coverage_row_matches_claim(row,state)
    finally: d._platform_pg_conn=old

def test_claim_loss_gap(c):
    for terminal in ("FAILED","SUCCESS"):
      state,p=seed(c,hstatus=terminal); before,_=rows(c)
      try: fin(c,p); assert False
      except d.CoverageFinalizationError as e: assert e.code==d.TRIPS_HISTORY_CLAIM_LOST
      after,h=rows(c); assert after==before and h["status"]==terminal
    state,p=seed(c); p.gate_result=gate(False)
    d._finalize_compat_gap(c,prepared=p)
    cov,h=rows(c)
    assert cov["bootstrap_status"]=="GAP_DETECTED"
    assert cov["last_gap_detected_ts"]==cov["updated_at"]
    assert (cov["coverage_start_ts"],cov["covered_through_ts"],cov["covered_through_source"])==(A,W,"bootstrap")
    assert h["status"]=="FAILED" and h["error_summary"]==TRIPS_COVERAGE_GAP_DETECTED
    assert p.coverage_state is state

def main():
    dsn=os.getenv(ENV)
    if not dsn: print(f"SKIP: set {ENV} to disposable PostgreSQL 16"); return
    # Destructive: drops the control-plane schema and reapplies migrations. The
    # substring check below is a name heuristic and proves nothing about where
    # the DSN points; the loopback guard is the actual gate, and it runs first.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower(): raise RuntimeError("refusing production-like DSN")
    import psycopg
    with psycopg.connect(dsn) as c:
      bootstrap(c); test_success(c); test_cas(c,dsn); test_claim_loss_gap(c)
    print("OK - C6 coverage finalization PostgreSQL checks passed")
if __name__=="__main__": main()
