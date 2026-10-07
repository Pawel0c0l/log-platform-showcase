#!/usr/bin/env python3
"""C6 rollback and commit-reconciliation tests on disposable PostgreSQL 16."""
from __future__ import annotations
import os, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"ops/tests_manual"))
from jobs.api.telematics import dispatcher as d
import test_telematics_coverage_finalization_postgres as base
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit

ENV="TELEMATICS_C6_CONCURRENCY_TEST_DSN"

def original_pair(c):
    state,p=base.seed(c)
    cov,h=base.rows(c)
    return state,p,cov,h

def test_rollback(c,dsn):
    import psycopg
    old=d._platform_pg_conn; d._platform_pg_conn=lambda: psycopg.connect(dsn)
    try:
        _,p,before,_=original_pair(c)
        c.execute("""CREATE OR REPLACE FUNCTION workflow_a_control.c6_fail_success()
          RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.status='SUCCESS' THEN RAISE EXCEPTION 'forced c6 crash'; END IF;
            RETURN NEW;
          END $$""")
        c.execute("""CREATE TRIGGER c6_fail_success
          BEFORE UPDATE ON workflow_a_control.client_schedule_run_history
          FOR EACH ROW EXECUTE FUNCTION workflow_a_control.c6_fail_success()""")
        c.commit()
        try:
            base.fin(c,p)
            assert False
        except d.CoverageFinalizationError as e:
            assert e.code==d.TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED
        after,h=base.rows(c)
        assert after==before
        assert h["status"]=="FAILED"
        c.execute("DROP TRIGGER c6_fail_success ON workflow_a_control.client_schedule_run_history")
        c.execute("DROP FUNCTION workflow_a_control.c6_fail_success()")
        c.commit()
    finally:
        d._platform_pg_conn=old

def reconcile(c,dsn):
    import psycopg
    old=d._platform_pg_conn; d._platform_pg_conn=lambda: psycopg.connect(dsn)
    try:
        # Expected committed success pair.
        _,p,orig_cov,orig_hist=original_pair(c)
        base.fin(c,p)
        expected_cov,_=base.rows(c)
        d._reconcile_compat_commit(
          branch="success",run_history_id=p.run_history_id,
          original_coverage=orig_cov,expected_coverage=expected_cov,
          original_history=orig_hist,expected_history_status="SUCCESS",
          expected_error_summary=None,business_subprocess_succeeded=True)

        # Expected committed gap pair.
        _,p,orig_cov,orig_hist=original_pair(c); p.gate_result=base.gate(False)
        d._finalize_compat_gap(c,prepared=p)
        expected_cov,_=base.rows(c)
        d._reconcile_compat_commit(
          branch="gap",run_history_id=p.run_history_id,
          original_coverage=orig_cov,expected_coverage=expected_cov,
          original_history=orig_hist,expected_history_status="FAILED",
          expected_error_summary=base.TRIPS_COVERAGE_GAP_DETECTED,
          business_subprocess_succeeded=False)

        # Exact original pair: separate guarded FAILED finalization.
        _,p,orig_cov,orig_hist=original_pair(c)
        fake=dict(orig_cov); fake["covered_through_ts"]=base.WE
        try:
          d._reconcile_compat_commit(
            branch="success",run_history_id=p.run_history_id,
            original_coverage=orig_cov,expected_coverage=fake,
            original_history=orig_hist,expected_history_status="SUCCESS",
            expected_error_summary=None,business_subprocess_succeeded=True)
          assert False
        except d.CoverageFinalizationError as e:
          assert e.code==d.TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED
        _,h=base.rows(c); assert h["status"]=="FAILED"

        # Terminal mismatch with original coverage.
        _,p,orig_cov,orig_hist=original_pair(c)
        c.execute("""UPDATE workflow_a_control.client_schedule_run_history
          SET status='SUCCESS',finished_at=now() WHERE run_history_id=%s""",
          (p.run_history_id,)); c.commit()
        try:
          d._reconcile_compat_commit(
            branch="success",run_history_id=p.run_history_id,
            original_coverage=orig_cov,expected_coverage=fake,
            original_history=orig_hist,expected_history_status="FAILED",
            expected_error_summary="different",business_subprocess_succeeded=True)
          assert False
        except d.CoverageFinalizationError as e:
          assert e.code==d.TRIPS_HISTORY_CLAIM_LOST

        # Coverage movement without terminal history is impossible divergence.
        _,p,orig_cov,orig_hist=original_pair(c)
        c.execute("""UPDATE workflow_a_control.client_dataset_coverage
          SET covered_through_ts=%s,covered_through_source='scheduled_run'
          WHERE schedule_id=%s""",(base.WE,base.SID)); c.commit()
        moved,_=base.rows(c)
        try:
          d._reconcile_compat_commit(
            branch="success",run_history_id=p.run_history_id,
            original_coverage=orig_cov,expected_coverage=moved,
            original_history=orig_hist,expected_history_status="SUCCESS",
            expected_error_summary=None,business_subprocess_succeeded=True)
          assert False
        except d.CoverageFinalizationError as e:
          assert e.code==d.TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE

        # Reconciliation unavailable performs no blind FAILED write.
        _,p,orig_cov,orig_hist=original_pair(c)
        d._platform_pg_conn=lambda: (_ for _ in ()).throw(OSError("offline"))
        try:
          d._reconcile_compat_commit(
            branch="success",run_history_id=p.run_history_id,
            original_coverage=orig_cov,expected_coverage=fake,
            original_history=orig_hist,expected_history_status="SUCCESS",
            expected_error_summary=None,business_subprocess_succeeded=True)
          assert False
        except d.CoverageFinalizationError as e:
          assert e.code==d.TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE
        _,h=base.rows(c); assert h["status"]=="RUNNING"
    finally:
        d._platform_pg_conn=old

def main():
    dsn=os.getenv(ENV)
    if not dsn: print(f"SKIP: set {ENV} to disposable PostgreSQL 16"); return
    # Destructive: bootstraps the control-plane schema. Loopback-only first.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "logdb" in dsn.lower(): raise RuntimeError("refusing production-like DSN")
    import psycopg
    with psycopg.connect(dsn) as c:
      base.bootstrap(c); test_rollback(c,dsn); reconcile(c,dsn)
    print("OK - C6 coverage concurrency/reconciliation PostgreSQL checks passed")
if __name__=="__main__": main()
