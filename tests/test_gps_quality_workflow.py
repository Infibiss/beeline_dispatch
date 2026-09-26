"""Synthetic positions only: conflicts, recovery, worksite evidence and approved delay."""
from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import threading
import pytest

from field_router.gps.config import GpsConfig
from field_router.gps.contracts import GpsInputError, validate_packet
from field_router.gps.context import project_context
from field_router.gps.delay import draft, confirm
from field_router.gps.models import BaselineGpsModel
from field_router.gps.service import GpsService
from field_router.gps.test_plan import make_test_day
from field_router.replanning import ScenarioSession
from test_gps import StoreOnly, service, NOW, point, packet, context, codes, wait_for
from test_gps_test_plan import worker, POINT, NOW as DAY
from test_auth import Client, PASSWORD
from test_gps_production import native_server,native_request


def fix(now,offset=.01,provider='gps',**extra):
    return {**point(now,offset=offset), 'provider':provider,'is_mock':False,**extra}


def test_provider_mock_and_native_receipt_survive_protocol_and_storage(native_server):
    server,_,_=native_server
    employee=Client(server);employee.login('worker')
    token=employee.request('/api/gps/mobile',{'action':'issue'})[1]['device_identifier']
    clock=[server.gps.service.clock()];server.gps.service.clock=lambda:clock[0]
    data={'id':token,'lat':55.7,'lon':37.7,'accuracy':8,'timestamp':clock[0],'provider':'gps','mock':'false'}
    status,result=native_request(server,data)
    assert status==200 and result['accepted'] and result['usable']
    clock[0]+=10
    status,result=native_request(server,{**data,'timestamp':clock[0],'lat':56.2,'provider':'network'})
    assert status==200 and result['accepted'] is False and result['reason']=='jump'
    assert 'км/ч' not in result['detail']
    assert 'противоречивые' in result['detail']
    assert server.gps.service.history_page('e1')['samples'][0]['point']['provider']=='network'
    assert native_request(server,{**data,'provider':'invented'})[0]==400
    clock[0]+=10
    result=native_request(server,{**data,'timestamp':clock[0],'mock':'true'})[1]
    assert result['accepted'] is False and result['reason']=='mock'
    assert employee.request('/api/gps/delay',{'version':1,'engineer_id':'e1'})[0]==403
    assert employee.request('/api/gps/confirm-delay',{})[0]==403


def test_conflicting_sources_suspend_forecast_even_when_old_cluster_returns():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        gps.ingest(packet(fix(clock[0],provider='network')))
        clock[0]+=10
        assert gps.ingest(packet(fix(clock[0],offset=.51)))['reason']=='jump'
        clock[0]+=10
        result=gps.ingest(packet(fix(clock[0],provider='network')))
        assert result['accepted'] and result['usable'] is False and result['reason']=='source_conflict'
        row=gps.snapshot()['rows'][0]
        assert row['analysis'] is None and row['quality_reason']=='source_conflict'
        assert 'противоречивые' in next(a for a in row['alerts'] if a['code']=='quality')['detail']
        clock[0]+=61
        result=gps.ingest(packet(fix(clock[0],provider='network')))
        assert result['usable'] and result['reason'] is None


def test_recovery_requires_stale_anchor_consistent_series_and_survives_restart(tmp_path):
    clock=[NOW];path=tmp_path/'gps.sqlite3'
    with service(clock=lambda:clock[0],history_path=path) as gps:
        gps.ingest(packet(fix(clock[0])))
        for seconds in (60,75,90):
            clock[0]=NOW+seconds
            result=gps.ingest(packet(fix(clock[0],offset=.51)))
            assert result['accepted'] is (seconds==90)
        assert len(gps._streams[('device','e1')]['points'])==1
        assert 'reacquired' in codes(gps.snapshot()['rows'][0])
    with service(clock=lambda:clock[0],history_path=path) as gps:
        assert len(gps._streams[('device','e1')]['points'])==1
        assert gps.snapshot()['rows'][0]['position']['lat']==pytest.approx(56.21)
        clock[0]+=10
        assert gps.ingest(packet(fix(clock[0],offset=.51)))['usable']


def test_unstable_rejected_points_cannot_reanchor():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        gps.ingest(packet(fix(clock[0])))
        for i,offset in enumerate((.51,.54,.58,.55,.59)):
            clock[0]=NOW+100+i*15
            assert not gps.ingest(packet(fix(clock[0],offset=offset)))['accepted']
        assert 'reacquired' not in codes(gps.snapshot()['rows'][0])


def test_worksite_mismatch_needs_multiple_recent_points_after_report():
    engineer=deepcopy(context()['engineers']['e1'])
    engineer['status']={'status':'working','request_id':'a','remaining_minutes':30,'time':540}
    def analyze(points,now=NOW+45):
        return BaselineGpsModel().analyze({'engineer':engineer,'date':'2026-09-20','now':now,
            'demo_minute':None,'routing':{},'config':GpsConfig().public(),'points':points})
    assert 'worksite_mismatch' in codes(analyze([fix(NOW),fix(NOW+20),fix(NOW+40)]))
    assert 'worksite_mismatch' not in codes(analyze([fix(NOW+40)]))
    assert 'worksite_mismatch' not in codes(analyze([fix(NOW,offset=.0001),fix(NOW+20,offset=.0001),fix(NOW+40,offset=.0001)]))
    assert 'worksite_mismatch' not in codes(analyze([fix(NOW),fix(NOW+20),fix(NOW+40)],NOW+200))
    engineer['status']['time']=541
    assert 'worksite_mismatch' not in codes(analyze([fix(NOW),fix(NOW+20),fix(NOW+40)]))


def traveling_session():
    s=make_test_day(worker('car'),POINT,DAY)
    s.record_fact({'version':s.version,'time':s.time,'engineer_id':'test-e1','status':'traveling',
                   'request_id':'gps-test-001','location_id':'office','remaining_minutes':5})
    return s


def test_delay_confirmation_affects_planner_only_after_explicit_action(tmp_path):
    s=traveling_session();before=s.dump()
    clock=[DAY.timestamp()+120]
    with closing(GpsService(project_context(s),clock=lambda:clock[0],history_path=tmp_path/'gps.sqlite3')) as gps:
        gps.ingest_native('test-e1','native-test',{**POINT,'measured_at_ms':clock[0]*1000,'provider':'gps'})
        wait_for(lambda:gps.snapshot()['rows'][0]['analysis'] is not None)
        proposal=gps.delay_draft({'version':s.version,'engineer_id':'test-e1'})
        assert s.dump()==before
        assert proposal['affected'][0]['shift_minutes']>0
        payload={k:proposal[k] for k in ('version','engineer_id','request_id','time','location_id','remaining_minutes','comment')}
        payload.update(status='traveling',gps_point_measured_at_ms=proposal['point_measured_at_ms'])
        shadow=ScenarioSession.restore(s.dump());confirm(shadow,gps,payload)
        assert shadow.needs_replanning
        assert shadow.statuses['test-e1']['remaining_minutes']==proposal['remaining_minutes']
        # Existing route remains unchanged until a chosen preview is approved.
        assert shadow.plan==s.plan
        from field_router.replanning import Event
        residual,_,_=shadow._residual(Event(id='gps-delay-check',type='replan',time=proposal['time']),False)
        assert residual  # Existing constraints, not a second GPS optimizer.
        clock[0]+=100
        with pytest.raises(GpsInputError):confirm(ScenarioSession.restore(s.dump()),gps,payload)
        assert s.dump()==before


def test_wrong_version_and_non_travel_status_never_generate_delay():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        with pytest.raises(GpsInputError):gps.delay_draft({'version':999,'engineer_id':'e1'})
        with pytest.raises(GpsInputError):gps.delay_draft({'version':1,'engineer_id':'e1'})


def test_delay_projection_waiting_absorbs_downstream_shift():
    value=context();engineer=value['engineers']['e1']
    engineer['status'].update(time=540,location_id='office')
    engineer['tasks'].append({'id':'b','address':'B','point':{'lat':55.71,'lon':37.7},
        'start':600,'end':620,'windows':[{'start':590,'end':650}],'duration_minutes':20,'travel_minutes':5})
    snapshot={'config':{'stale_seconds':90},'rows':[{'engineer_id':'e1','date_matches':True,
        'clock':{'minute':551},'position':point(NOW+660),'point_age_seconds':0,
        'analysis':{'target':{'id':'a','basis':'confirmed'},'eta':{'low_minutes':3,'high_minutes':5}}}]}
    proposal=draft(value,snapshot,'e1')
    assert proposal['affected'][0]['shift_minutes']==6
    assert proposal['affected'][1]['shift_minutes']==0
    assert not any(a['window_missed'] for a in proposal['affected'])
