#!/usr/bin/env python3
"""DB/network-free integration, idempotency, and migration assertions."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace

from jobs.ecodriving import email_idempotency as driver_idem
from jobs.ecodriving_person import email_idempotency as person_idem
from jobs.ecodriving import job_eco_driving_weekly_email_notifications as dw
from jobs.ecodriving import job_eco_driving_monthly_email_notifications as dm
from jobs.ecodriving_person import job_eco_driving_person_weekly_email_notifications as pw
from jobs.ecodriving_person import job_eco_driving_person_monthly_email_notifications as pm

ROOT=Path(__file__).resolve().parents[2]
CLIENT='00000000-0000-0000-0000-000000000001'

class Cur:
 def __init__(self): self.queries=[]; self.results=[]; self.rowcount=0
 def execute(self,q,args=None): self.queries.append((q,args)); return self
 def fetchone(self): return self.results.pop(0) if self.results else None
 def fetchall(self): return []
 def __enter__(self): return self
 def __exit__(self,*a): pass
class Conn:
 def __init__(self): self.cur=Cur(); self.commits=0
 def cursor(self,*a,**k): return self.cur
 def commit(self): self.commits+=1
 def rollback(self): pass
 def close(self): pass
class Client:
 def __init__(self): self.logs=[]
 def log(self,*a,**k): self.logs.append((a,k))

def patch_render_only(mod, person=False, monthly=False):
 conn=Conn(); smtp=[]
 mod._load_client_account_config=lambda **k: SimpleNamespace(client_db_schema='public',client_code='BRAVO00016' if person else 'ALPHA00001')
 mod._client_business_pg_conn=lambda cfg: conn
 mod.validate_template_inventory=lambda p: None
 mod._table_columns=lambda *a,**k: {'email','person_name'} if person else {'email','driver_name'}
 mod._select_email_column=lambda c: 'email'
 start,end,label = ((date(2026,6,1),date(2026,7,1),'2026-06') if monthly else (date(2026,7,1),date(2026,7,20),'W3'))
 mod.fetch_period_candidates=lambda *a,**k: [{'period_start_date':start,'period_end_date':end,'period_label':label,'snapshot_min_updated_at':datetime.combine(end,datetime.min.time(),ZoneInfo('Europe/Warsaw'))+timedelta(minutes=1),'snapshot_max_updated_at':datetime.combine(end,datetime.min.time(),ZoneInfo('Europe/Warsaw'))+timedelta(minutes=1)}]
 row={'client_id':CLIENT,'period_start_date':start,'period_end_date':end,
      'recipient_email':'real@example.test','ecodriving_rating_type':'bezpieczny','ranking_included':False,
      'qualification_status':'OK','ranking_type':'EXCLUDED'}
 if person: row.update(person_name_group_key='person',person_name='Person')
 else: row.update(assigned_id='driver')
 mod._fetch_candidates=lambda *a,**k: [dict(row)]
 mod.classify_candidate=lambda *a,**k: mod.CandidateDecision(None,'bezpieczny','template.html','norank')
 mod._load_template=lambda **k: '<p>x</p>'
 mod.build_template_context=lambda r: {}
 mod.render_template=lambda *a,**k: '<p>rendered</p>'
 mod.render_subject=lambda *a,**k: 'subject'
 mod._already_sent=lambda *a,**k: (_ for _ in ()).throw(AssertionError('render-only checked normal idempotency'))
 mod.send_html_email=lambda *a,**k: smtp.append(k) or '<id@test>'
 mod.submit_prepared_email=lambda *a,**k: smtp.append(k) or (_ for _ in ()).throw(AssertionError('render-only submitted SMTP'))
 mod._sent_archive_settings=lambda *a,**k: (_ for _ in ()).throw(AssertionError('render-only loaded IMAP'))
 if person and not monthly:
  mod.load_smtp_settings_from_env=lambda *,dry_run,client_code: SimpleNamespace(env_prefix='TEST',from_name='x',from_email='x@example.test') if dry_run else (_ for _ in ()).throw(AssertionError('render-only loaded SMTP secrets'))
 result=mod.run(Client(),'run-render',{'client_id':CLIENT})
 assert result['execution_mode']=='render_only'
 assert result['rendered_count']==1 and result['sent_count']==0
 assert smtp==[]
 assert all('pending' not in q.lower() for q,_ in conn.cur.queries)

def test_default_render_only_all_four():
 for mod,person,monthly in ((dw,False,False),(dm,False,True),(pw,True,False),(pm,True,True)):
  patch_render_only(mod,person,monthly)

def test_stable_keys_and_stale_pending():
 a=person_idem.build_idempotency_key(report_type='weekly',client_id=CLIENT,person_name_group_key='p',period_start_date=date(2026,7,1),period_end_date=date(2026,7,20),template_type='old')
 b=person_idem.build_idempotency_key(report_type='weekly',client_id=CLIENT,person_name_group_key='p',period_start_date=date(2026,7,1),period_end_date=date(2026,7,20),template_type='new')
 assert a==b and 'old' not in a and 'new' not in b
 da=driver_idem.build_idempotency_key(client_id=CLIENT,assigned_id='d',report_type='weekly',period_start_date=date(2026,7,1),period_end_date=date(2026,7,20))
 assert 'template' not in da
 cur=Cur(); cur.results=[{'send_log_id':'00000000-0000-0000-0000-000000000009','status':'pending','attempted_at':'old','is_stale':True,'is_ambiguous':False}]
 row={'client_id':CLIENT,'person_name_group_key':'p','person_name':'P','period_start_date':date(2026,7,1),'period_end_date':date(2026,7,20)}
 decision=SimpleNamespace(template_type='changed',template_filename='x',template_variant='norank')
 res=person_idem.reserve_send(cur,send_log_table='public.log',report_type='weekly',run_id='r',row=row,decision=decision,recipient_email='x',original_recipient_email='x',subject='s',send_scope='normal',pending_stale_after_minutes=120,metadata_json={})
 assert res.outcome==person_idem.STALE_PENDING_REQUIRES_RECONCILIATION
 assert not any(q.lstrip().upper().startswith('UPDATE') for q,_ in cur.queries)

def test_migration_contract():
 sql=(ROOT/'db/client_business/044_eco_email_fail_closed_idempotency.sql').read_text()
 assert sql.index('ECO_EMAIL_IDEMPOTENCY_CONFLICT') < sql.index("identity_index :=")
 assert "to_regclass('public.' || r.table_name)" in sql
 assert 'ECO_EMAIL_TABLE_NOT_APPLICABLE' in sql
 assert 'ON public.eco_person_' not in sql and 'ON public.eco_driving_' not in sql
 for subject in ('assigned_id','person_name_group_key'):
  assert subject in sql
 assert "status IN ('pending','sent')" in sql
 assert "send_scope='normal'" in sql
 index_statement=sql.split("EXECUTE format('CREATE UNIQUE INDEX %I ON public.%I",1)[1].split(";",1)[0]
 assert 'template_type' not in index_statement
 for detail in ('model=%','table=%','row_count','statuses','scopes','template_types','row_ids'):
  assert detail in sql
 assert 'DELETE FROM' not in sql.upper()

def test_sender_sources():
 for mod in (dw,dm,pw,pm):
  src=Path(mod.__file__).read_text()
  assert 'resolve_execution_contract(params)' in src
  assert 'select_period_for_send' in src
  assert 'def _latest_period' not in src
  assert 'execution.mode is ExecutionMode.NORMAL_SEND' in src

def main():
 test_default_render_only_all_four(); test_stable_keys_and_stale_pending(); test_migration_contract(); test_sender_sources()
 print('OK - four sender fail-closed integration/idempotency/migration checks')
if __name__=='__main__': main()
