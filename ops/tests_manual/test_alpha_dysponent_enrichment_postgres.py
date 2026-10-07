#!/usr/bin/env python3
from __future__ import annotations
import os
from datetime import date
import psycopg
from psycopg.rows import dict_row
from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as job

CLIENT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
RAW = "33333333-3333-3333-3333-333333333333"
RUN = "44444444-4444-4444-4444-444444444444"


def setup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute('DROP SCHEMA IF EXISTS telematics_reports CASCADE')
        cur.execute('DROP TABLE IF EXISTS public.client_trips CASCADE')
        cur.execute('DROP TABLE IF EXISTS public.eco_drivers_id_chart CASCADE')
        cur.execute('CREATE SCHEMA telematics_reports')
        cur.execute('''CREATE TABLE telematics_reports."Alpha_GPS_Baza_LOG" (
          source_id text, registration text, assignment_date date, imported_at timestamptz,
          raw_file_id uuid, workflow_run_id uuid, source_row_number integer)''')
        cur.execute('''CREATE TABLE public.client_trips (
          client_id uuid, provider_trip_id bigint, registration text, start_timestamp timestamptz,
          trip_distance_meters integer, "Driver_Restrictions" text, "Dysponent_ID" text,
          driver_tag_description text, PRIMARY KEY(client_id,provider_trip_id))''')
        cur.execute('''CREATE TABLE public.eco_drivers_id_chart (
          client_id uuid, driver_id text, is_active boolean,
          PRIMARY KEY(client_id,driver_id))''')
        source_rows = [(' A1 ',' aa 1 ','2026-03-01',1), ('A2','AA1','2026-03-29',2),
                       ('B1','BB2','2026-03-01',3), ('B2','bb 2','2026-03-01',4),
                       ('','CC3','2026-03-01',5), ('D1','DD4','2026-03-01',6)]
        for source_id, registration, assigned, row_number in source_rows:
            cur.execute('''INSERT INTO telematics_reports."Alpha_GPS_Baza_LOG"
              VALUES (%s,%s,%s,'2026-03-31 10:00+02',%s,%s,%s)''',
              (source_id, registration, assigned, RAW, RUN, row_number))
        trips = [
          (CLIENT,1,'AA1','2026-03-28 23:30+01',1000,None,None,None),
          (CLIENT,2,'a a 1','2026-03-29 03:30+02',2000,None,None,None),
          (CLIENT,3,'AA1','2026-03-30 10:00+02',3000,None,None,None),
          (CLIENT,4,'BB2','2026-03-30 10:00+02',4000,None,None,None),
          (CLIENT,5,'CC3','2026-03-30 10:00+02',5000,None,None,None),
          (CLIENT,6,'DD4','2026-03-30 10:00+02',6000,None,'OLD',None),
          (CLIENT,7,'AA1','2026-03-30 10:00+02',7000,'DIRECT',None,None),
          (CLIENT,8,'AA1','2026-04-01 00:00+02',8000,None,None,None),
          (CLIENT,9,'AA1','2026-03-30 10:00+02',9000,None,None,'prywatny'),
          (OTHER,1,'AA1','2026-03-30 10:00+02',10000,None,None,None)]
        cur.executemany('INSERT INTO public.client_trips VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', trips)
        cur.executemany('INSERT INTO public.eco_drivers_id_chart VALUES (%s,%s,true)',
                        [(CLIENT,'A1'),(CLIENT,'A2'),(CLIENT,'D1'),(CLIENT,'DIRECT')])
    conn.commit()


def params(overwrite=False):
    return job._parse_params({'date_from':'2026-03-28','date_to':'2026-04-01',
        'overwrite_existing':overwrite,'process_all':True,'batch_size':2,
        'max_source_age_hours':100000})


def win():
    return job.ResolvedWindow(date(2026,3,28),date(2026,4,1),date(2026,3,28),date(2026,4,1),
                              date(2026,4,1),date(2026,4,2),False,False)


def cfg():
    return job.ClientDbConfig('ALPHA00001',CLIENT,'x',5432,'fixture','x','x')


def test_matching_and_updates(conn) -> None:
    with conn.cursor() as cur:
        values = job._analyze_scope(cur, config=cfg(), params=params(), window=win())
    conn.rollback()
    assert values['target_trips_in_scope'] == 7
    assert values['ambiguous_source_matches'] == 1 and values['no_source_match'] == 1
    assert values['conflicting_existing_target_values'] == 1 and values['planned_updates'] == 4
    total, batches = job._execute_batches(conn, config=cfg(), params=params(), window=win(),
        expected_source_raw_file_id=RAW, client=None, run_id='fixture')
    assert total == 4 and batches == 3
    with conn.cursor() as cur:
        cur.execute('SELECT provider_trip_id,"Dysponent_ID" FROM public.client_trips WHERE client_id=%s ORDER BY provider_trip_id',(CLIENT,))
        found={row['provider_trip_id']:row['Dysponent_ID'] for row in cur.fetchall()}
    conn.rollback()
    assert found[1]=='A1' and found[2]=='A2' and found[3]=='A2'
    assert found[4] is None and found[5] is None and found[6]=='OLD' and found[7]=='A2'
    assert job._execute_batches(conn, config=cfg(), params=params(), window=win(),
        expected_source_raw_file_id=RAW, client=None, run_id='fixture')[0] == 0
    assert job._execute_batches(conn, config=cfg(), params=params(True), window=win(),
        expected_source_raw_file_id=RAW, client=None, run_id='fixture')[0] == 1
    with conn.cursor() as cur:
        cur.execute('SELECT "Dysponent_ID" FROM public.client_trips WHERE client_id=%s AND provider_trip_id=6',(CLIENT,))
        assert cur.fetchone()['Dysponent_ID']=='D1'
        cur.execute('SELECT "Dysponent_ID" FROM public.client_trips WHERE client_id=%s AND provider_trip_id=1',(OTHER,))
        assert cur.fetchone()['Dysponent_ID'] is None
        cur.execute('SELECT "Dysponent_ID" FROM public.client_trips WHERE client_id=%s AND provider_trip_id=8',(CLIENT,))
        assert cur.fetchone()['Dysponent_ID'] is None
    conn.rollback()


def test_dry_run_and_partial_commit(conn) -> None:
    setup(conn)
    with conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        assert job._analyze_scope(cur, config=cfg(), params=params(), window=win())['planned_updates'] == 4
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM public.client_trips WHERE "Dysponent_ID" IS NOT NULL')
        assert cur.fetchone()['n'] == 1
    conn.rollback()
    original = job._update_batch
    calls = 0
    def fail_second(cur, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2: raise RuntimeError('fixture batch failure')
        return original(cur, **kwargs)
    job._update_batch = fail_second
    try:
        try:
            job._execute_batches(conn, config=cfg(), params=params(), window=win(),
                expected_source_raw_file_id=RAW, client=None, run_id='fixture')
        except RuntimeError: pass
        else: raise AssertionError('batch failure was swallowed')
    finally:
        job._update_batch = original
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM public.client_trips WHERE "Dysponent_ID" IS NOT NULL')
        assert cur.fetchone()['n'] == 3
    conn.rollback()


def main() -> None:
    dsn = os.environ.get('ALPHA_DYSPONENT_TEST_DSN')
    if not dsn: raise SystemExit('ALPHA_DYSPONENT_TEST_DSN must point to a disposable database')
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        setup(conn); test_matching_and_updates(conn); test_dry_run_and_partial_commit(conn)
    print('OK - isolated Postgres ALPHA Dysponent enrichment tests passed')


if __name__ == '__main__': main()
