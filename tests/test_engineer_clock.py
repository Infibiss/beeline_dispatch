"""Frozen server clock; profile reads do not mutate the saved plan or facts."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import http.client
import threading

import pytest

from field_router.engineer import engineer_state
from field_router.engineer_clock import operational_clock, require_current_write
from field_router.scenario_server import create_server
from test_auth import Client
from test_engineer_unavailability import session_with_route, report


def instant(hour, minute=0, *, day=17, month=8):
    return datetime(2026,month,day,hour,minute,tzinfo=timezone(timedelta(hours=3)))


@contextmanager
def running(session, tmp_path, clock):
    path=tmp_path/'clock-state.json'
    server=create_server(session,path,port=0,disable_gps=True,
                         engineer_clock_now=lambda:clock[0])
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        yield Client(server),path
    finally:
        server.shutdown();server.server_close();thread.join()


def test_today_clock_uses_actual_moscow_time_even_when_saved_plan_ahead(make_scenario):
    session=session_with_route(make_scenario);session.time=650
    clock=operational_clock(session,instant(10))
    assert clock=={'mode':'live','date':'2026-08-17','time':600,'plan_time':650,
                   'server_date':'2026-08-17','server_time':600,'timezone':'Europe/Moscow',
                   'key':'live/2026-08-17/600'}
    with pytest.raises(ValueError,match='опережает'):
        require_current_write(session,{'version':session.version},clock)
    with pytest.raises(ValueError,match='версия'):
        require_current_write(session,{'version':session.version-1},clock)


def test_clock_date_conversion_and_archived_day_are_explicit(make_scenario):
    session=session_with_route(make_scenario)
    local_midnight=datetime(2026,8,16,21,15,tzinfo=timezone.utc)
    assert operational_clock(session,local_midnight)['mode']=='live'
    assert operational_clock(session,local_midnight.timestamp())['time']==15
    clock=operational_clock(session,instant(16,38,day=28,month=9))
    assert clock['mode']=='plan' and clock['time']==540
    assert clock['server_time']==998 and clock['server_date']=='2026-09-28'
    assert clock['date']=='2026-08-17'
    with pytest.raises(ValueError,match='часовой пояс'):
        operational_clock(session,datetime(2026,8,17))


def test_profile_absence_starts_and_ends_without_mutation_or_version_change(make_scenario,tmp_path):
    session=session_with_route(make_scenario)
    report(session,start_time='09:10',end_time='10:00',location_id=None)
    before=deepcopy(session.dump());clock=[instant(9,5)]
    with running(session,tmp_path,clock) as (client,path):
        status,first,_=client.request('/api/engineer?engineer_id=e1')
        assert status==200 and first['time']==545 and first['plan_time']==540
        assert first['active_absence'] is None and first['scheduled_absences']
        revision=client.request('/api/revision')[1]
        clock[0]=instant(9,10)
        active=client.request('/api/engineer?engineer_id=e1')[1]
        next_revision=client.request('/api/revision')[1]
        assert active['active_absence']['is_active'] and not active['scheduled_absences']
        assert revision['version']==next_revision['version']==first['version']
        assert revision['clock']['key']!=next_revision['clock']['key']
        clock[0]=instant(10)
        ended=client.request('/api/engineer?engineer_id=e1')[1]
        assert ended['active_absence'] is None and ended['return_confirmation_required']
        assert ended['inventory']==first['inventory']
        assert ended['facts']==first['facts']
        assert session.dump()==before
        assert not path.exists()


def test_stale_now_form_is_resolved_again_on_server(make_scenario,tmp_path):
    session=session_with_route(make_scenario);clock=[instant(9,1)]
    with running(session,tmp_path,clock) as (client,path):
        first=client.request('/api/engineer?engineer_id=e1')[1]
        clock[0]=instant(10)
        status,data,_=client.request('/api/engineer/unavailability',{
            'version':first['version'],'engineer_id':'e1','start_mode':'now',
            'start_date':'2026-08-17','start_time':'09:01',
            'end_date':'2026-08-17','end_time':'11:00','reason':'sick','comment':'Болезнь'})
        assert status==200,data
        assert data['active_absence']['start_time']=='10:00'
        assert data['clock']['time']==600 and data['plan_time']==540
        saved=json.loads(path.read_text(encoding='utf-8'))
        assert saved['time']==540 and saved['fact_log']==[]
        assert saved['unavailability'][0]['location_id'] is None
        assert saved['unavailability'][0]['start_time']=='10:00'


def test_live_fact_uses_server_time_and_ahead_clock_rejection_is_atomic(make_scenario,tmp_path):
    session=session_with_route(make_scenario);clock=[instant(9,1)]
    with running(session,tmp_path,clock) as (client,path):
        status,data,_=client.request('/api/engineer/facts',{'version':session.version,
            'engineer_id':'e1','status':'available','time':800,'location_id':'office'})
        assert status==200,data
        assert data['status']['time']==541 and data['time']==541
        prior_bytes=path.read_bytes()
        clock[0]=instant(9)
        status,error,_=client.request('/api/engineer/facts',{'version':data['version'],
            'engineer_id':'e1','status':'available','time':900,'location_id':'office'})
        assert status==400 and 'опережает' in error['error']
        assert path.read_bytes()==prior_bytes
        status,error,_=client.request('/api/engineer/facts',{'version':data['version']-1,
            'engineer_id':'e1','status':'available','time':900,'location_id':'office'})
        assert status==409 and 'версия' in error['error']


def test_historical_plan_retains_its_date_and_fact_time(make_scenario,tmp_path):
    session=session_with_route(make_scenario);clock=[instant(16,38,day=28,month=9)]
    with running(session,tmp_path,clock) as (client,path):
        data=client.request('/api/engineer?engineer_id=e1')[1]
        assert data['clock']['mode']=='plan' and data['time']==540
        assert data['date']=='2026-08-17'
        status,data,_=client.request('/api/engineer/facts',{'version':data['version'],
            'engineer_id':'e1','status':'available','time':545,'location_id':'office'})
        assert status==200,data
        assert data['status']['time']==545 and data['clock']['time']==545
        assert json.loads(path.read_text(encoding='utf-8'))['scenario']['date']=='2026-08-17'


def test_edit_and_cancel_classify_active_period_by_current_server_time(make_scenario,tmp_path):
    session=session_with_route(make_scenario)
    report(session,start_time='10:00',end_time='11:00',location_id=None)
    clock=[instant(10,10)]
    with running(session,tmp_path,clock) as (client,path):
        data=client.request('/api/engineer?engineer_id=e1')[1]
        period=data['active_absence']
        assert period['is_active']
        payload={key:period[key] for key in ('engineer_id','start_date','start_time',
            'end_date','end_time','reason','comment')}
        payload.update(version=data['version'],report_id=period['id'],start_time='10:01')
        status,error,_=client.request('/api/engineer/unavailability/update',payload)
        assert status==400 and 'Начало текущего' in error['error']
        assert not path.exists()
        status,data,_=client.request('/api/engineer/unavailability/cancel',{
            'version':data['version'],'engineer_id':'e1','report_id':period['id'],
            'correction_reason':'Отправлено по ошибке'})
        assert status==200,data
        assert data['status']['time']==610 and data['status']['location_id'] is None
        assert data['location_confirmation_pending'] and data['active_absence'] is None


def test_live_return_time_cannot_be_backdated_by_an_open_form(make_scenario,tmp_path):
    session=session_with_route(make_scenario)
    report(session,start_time='10:00',end_time='11:00',location_id=None)
    clock=[instant(10,15)]
    with running(session,tmp_path,clock) as (client,path):
        status,data,_=client.request('/api/engineer/return',{
            'version':session.version,'engineer_id':'e1','report_id':session.unavailability[0]['id'],
            'time':541})
        assert status==200,data
        assert data['status']['time']==615 and data['clock']['time']==615
        assert data['location_confirmation_pending']
        assert not data['return_confirmation_required'] and data['active_absence'] is None
        saved=json.loads(path.read_text(encoding='utf-8'))
        assert saved['unavailability'][0]['end_time']=='10:15'
        assert saved['fact_log'][-1]['time']==615


def test_midnight_cannot_convert_an_open_live_form_into_a_historical_write(make_scenario,tmp_path):
    session=session_with_route(make_scenario);before=deepcopy(session.dump())
    clock=[instant(23,59)]
    with running(session,tmp_path,clock) as (client,path):
        current=client.request('/api/engineer?engineer_id=e1')[1]
        def post(context):
            connection=http.client.HTTPConnection('127.0.0.1',client.port,timeout=10)
            connection.request('POST','/api/engineer/facts',json.dumps({
                'version':current['version'],'engineer_id':'e1','status':'available',
                'time':545,'location_id':'office'}),headers={
                    'Content-Type':'application/json','X-Engineer-Clock':context})
            response=connection.getresponse();body=json.loads(response.read())
            status=response.status;connection.close();return status,body
        clock[0]=instant(0,1,day=18)
        status,error=post('live/2026-08-17/2026-08-17')
        assert status==409 and 'рабочего дня' in error['error']
        assert not path.exists() and session.dump()==before
        status,data=post('plan/2026-08-17/2026-08-18')
        assert status==200,data
        assert data['date']=='2026-08-17' and data['status']['time']==545
        assert data['clock']['mode']=='plan'


def test_live_work_and_absence_transitions_do_not_require_waiting_a_minute(make_scenario,tmp_path):
    session=session_with_route(make_scenario);clock=[instant(9,1)]
    with running(session,tmp_path,clock) as (client,path):
        data=client.request('/api/engineer?engineer_id=e1')[1]
        for fact in (
                {'status':'traveling','request_id':'a','location_id':'office','remaining_minutes':1},
                {'status':'working','request_id':'a','remaining_minutes':30},
                {'status':'interrupted','request_id':'a','remaining_minutes':30,
                 'consumed_router':0,'consumed_tv_box':0,'comment':'Не могу продолжить работу'}):
            status,data,_=client.request('/api/engineer/facts',{
                'version':data['version'],'engineer_id':'e1','time':900,**fact})
            assert status==200,data
            assert data['status']['time']==541
        status,data,_=client.request('/api/engineer/unavailability',{
            'version':data['version'],'engineer_id':'e1','start_mode':'now',
            'start_date':'2026-08-17','start_time':'09:01',
            'end_date':'2026-08-17','end_time':'10:00','reason':'sick','comment':'Болезнь'})
        assert status==200,data
        period=data['active_absence']
        assert period['start_time']=='09:01'
        status,data,_=client.request('/api/engineer/return',{
            'version':data['version'],'engineer_id':'e1','report_id':period['id'],'time':900})
        assert status==200,data
        assert data['status']['status']=='available' and data['status']['time']==541
        assert data['active_absence'] is None and data['location_confirmation_pending']
        saved=json.loads(path.read_text(encoding='utf-8'))
        assert [fact['status'] for fact in saved['fact_log']]==[
            'traveling','working','interrupted','available']
        assert {fact['time'] for fact in saved['fact_log']}=={541}
        sequences=[fact['sequence'] for fact in saved['fact_log']]
        assert sequences==sorted(set(sequences))
