#!/usr/bin/env python3
"""Read-only, identity-attested Eco email period/conflict inspection."""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from psycopg import connect
from psycopg.rows import dict_row
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.environment_identity import (ClientIdentityExpectation, attest_client_identity,
    attest_platform_identity, load_runtime_identity)
from jobs.ecodriving.email_safety import (EcoEmailPreconditionError, evaluate_period,
    select_latest_closed_period)

MODELS={
 "ALPHA00001": {"client_id":"9536f715-2fd0-4ffd-86ed-ba06f5490c5e","model":"driver",
  "weekly":("eco_driver_weekly_stats","eco_driving_weekly_email_send_log","eco_trip_assignments","assigned_id"),
  "monthly":("eco_driver_monthly_stats","eco_driving_monthly_email_send_log","eco_trip_assignments","assigned_id")},
 "BRAVO00016": {"client_id":"6018be20-5faa-41b6-89c9-fe2b54a8283e","model":"person",
  "weekly":("eco_person_weekly_stats","eco_person_weekly_email_send_log","eco_person_trip_assignments","person_name_group_key"),
  "monthly":("eco_person_monthly_stats","eco_person_monthly_email_send_log","eco_person_trip_assignments","person_name_group_key")},
}

def pg(**kw): return connect(**kw,row_factory=dict_row)
def main():
 load_dotenv(ROOT/'.env',override=False)
 ap=argparse.ArgumentParser(); ap.add_argument('--client-code',action='append',choices=sorted(MODELS)); args=ap.parse_args()
 codes=args.client_code or sorted(MODELS); runtime=load_runtime_identity()
 out={"inspection_only":True,"evaluated_clients":[],"conflicts":[]}
 with pg(host=os.environ['POSTGRES_HOST'],port=int(os.environ['POSTGRES_PORT']),dbname=os.environ['POSTGRES_DB'],user=os.environ['POSTGRES_USER'],password=os.getenv('POSTGRES_PASSWORD','')) as pc:
  pc.execute("SET TRANSACTION READ ONLY"); attest_platform_identity(pc,runtime)
  for code in codes:
   spec=MODELS[code]
   row=pc.execute("""SELECT client_id::text,client_code,client_db_host,client_db_port,client_db_name,client_db_user,
      client_db_password_secret_ref,client_db_schema,client_db_environment,client_db_identity_id::text
      FROM workflow_a_control.client_account WHERE enabled IS TRUE AND client_code=%s""",(code,)).fetchone()
   if not row or row['client_id']!=spec['client_id']: raise RuntimeError('CLIENT_IDENTITY_MISMATCH')
   with pg(host=row['client_db_host'],port=row['client_db_port'],dbname=row['client_db_name'],user=row['client_db_user'],password=resolve_secret(row['client_db_password_secret_ref'])) as cc:
    cc.execute("SET TRANSACTION READ ONLY")
    attest_client_identity(cc,runtime,ClientIdentityExpectation(code,row['client_db_environment'],row['client_db_identity_id'],row['client_db_name'],row['client_db_user']))
    client={"client_code":code,"client_id":row['client_id'],"model":spec['model'],"periods":[]}
    for report in ('weekly','monthly'):
     stats,logs,assign,subject=spec[report]
     if report=='weekly':
      q=f"""SELECT min(period_label) period_label,period_start_date,period_end_date,count(*) row_count,
       min(created_at) created_at,min(updated_at) snapshot_min_updated_at,max(updated_at) snapshot_max_updated_at FROM public.{stats}
       WHERE client_id=%s GROUP BY period_start_date,period_end_date ORDER BY period_end_date"""
     else:
      q=f"""SELECT to_char(month_start_date,'YYYY-MM') period_label,month_start_date period_start_date,
       month_end_date period_end_date,count(*) row_count,min(created_at) created_at,min(updated_at) snapshot_min_updated_at,max(updated_at) snapshot_max_updated_at
       FROM public.{stats} WHERE client_id=%s GROUP BY month_start_date,month_end_date ORDER BY month_end_date"""
     rows=[dict(x) for x in cc.execute(q,(row['client_id'],)).fetchall()]
     selection=None
     if rows:
      try: selection=select_latest_closed_period(rows,report_type=report,require_finalized_snapshot=True)
      except EcoEmailPreconditionError: pass
     old_end=max((x['period_end_date'] for x in rows),default=None)
     for x in rows:
      d=evaluate_period(x,report_type=report,require_finalized_snapshot=True)
      source=cc.execute(f"""SELECT min(trip_start_ts) min_source_trip_ts,max(trip_start_ts) max_source_trip_ts
       FROM public.{assign} WHERE client_id=%s AND aggregation_included IS TRUE
       AND trip_start_ts >= (%s::date::timestamp AT TIME ZONE 'Europe/Warsaw')
       AND trip_start_ts < (%s::date::timestamp AT TIME ZONE 'Europe/Warsaw')""",
       (row['client_id'],x['period_start_date'],x['period_end_date'])).fetchone()
      refs=cc.execute(f"SELECT count(*) n FROM public.{logs} WHERE client_id=%s AND period_start_date=%s AND period_end_date=%s",
       (row['client_id'],x['period_start_date'],x['period_end_date'])).fetchone()['n']
      client['periods'].append({"report_type":report,"period_label":x['period_label'],
       "period_start":x['period_start_date'],"exclusive_period_end":x['period_end_date'],
       "evaluated_warsaw":d.evaluated_at,"state":d.state,"rejection_code":d.rejection_code,
       "row_count":x['row_count'],"created_at":x['created_at'],"snapshot_min_updated_at":x['snapshot_min_updated_at'],"snapshot_max_updated_at":x['snapshot_max_updated_at'],
       **dict(source),"send_log_reference_count":refs,"old_logic_selected":x['period_end_date']==old_end,
       "new_logic_selected":bool(selection and selection.selected.period_start_date==x['period_start_date'] and selection.selected.period_end_date==x['period_end_date'])})
     if spec['model'] == 'driver':
      scope_expr = "CASE WHEN metadata_json->>'force_resend'='true' THEN 'forced' WHEN NULLIF(btrim(metadata_json->>'test_recipient_email'),'') IS NOT NULL THEN 'test' ELSE 'normal' END"
      conflict_where = "status='sent' AND metadata_json->>'force_resend' IS DISTINCT FROM 'true' AND NULLIF(btrim(metadata_json->>'test_recipient_email'),'') IS NULL"
     else:
      scope_expr = "send_scope"
      conflict_where = "send_scope='normal' AND status IN ('pending','sent')"
     conflicts=cc.execute(f"""SELECT client_id::text,%s report_model,{subject}::text subject_identity,report_type,
       period_start_date,period_end_date,count(*) row_count,array_agg(DISTINCT status) statuses,
       array_agg(DISTINCT {scope_expr}) scopes,array_agg(DISTINCT template_type) template_types,
       array_agg(DISTINCT ecodriving_rating_type) ratings,min(attempted_at) earliest_timestamp,
       max(attempted_at) latest_timestamp,array_agg(send_log_id::text ORDER BY attempted_at) row_ids
       FROM public.{logs} WHERE {conflict_where}
       GROUP BY client_id,{subject},report_type,period_start_date,period_end_date HAVING count(*)>1""",(spec['model'],)).fetchall()
     out['conflicts'].extend(dict(x) for x in conflicts)
    out['evaluated_clients'].append(client)
    cc.rollback()
  pc.rollback()
 print(json.dumps(out,ensure_ascii=False,default=str,indent=2))
if __name__=='__main__': main()
