"""Real clock, departure risks and contact freshness; all coordinates synthetic."""
from copy import deepcopy
import pytest
from field_router.gps.models import BaselineGpsModel
from field_router.gps.config import GpsConfig
from field_router.gps.service import GpsService
from test_gps import NOW, context, point, packet, codes, service, wait_for
from test_auth import Client
from test_gps_production import native_server, native_request


def result(minute, phase='available', *, demo=False, windows=None):
    engineer=deepcopy(context()['engineers']['e1'])
    engineer['status']={'status':phase,'request_id':'a'} if phase else None
    if windows:engineer['tasks'][0]['windows']=windows
    now=NOW+(minute-540)*60
    model=BaselineGpsModel()
    model.routing.estimate=lambda *args: {'kind':'road','minutes':5,'metres':1500,
        'description':'Детерминированный маршрут для проверки времени.',
        'legs':[{'mode':'car','origin':'Точка','destination':'Адрес','minutes':5,'metres':1500}]}
    return model.analyze({'engineer':engineer,'date':'2026-09-20','now':now,
        'demo_minute':minute if demo else None,'routing':{},'config':GpsConfig().public(),
        'points':[point(now-120),point(now-60),point(now)]})


def test_departure_due_for_free_worker_without_travel_fact():
    early=result(540)
    assert early['target']['basis']=='plan' and 'departure_due' not in codes(early)
    due=result(547)
    assert 'departure_due' in codes(due)
    assert due['eta']['schedule']['leave_by']==543
    assert due['eta']['schedule']['source']=='server'
    assert 'plan_late' in codes(result(551))
    assert 'late' not in codes(result(551))  # Planned start != end of client window.
    assert 'departure_due' in codes(result(547,phase=None))


def test_stationary_alert_explains_time_and_plan_impact():
    late=result(551,phase='traveling')
    stopped=next(a for a in late['alerts'] if a['code']=='stopped')
    assert '09:11' in stopped['detail'] and '09:10' in stopped['detail']
    assert 'позже плана' in stopped['detail']
    assert 'plan_late' in codes(late)


def test_demo_and_device_share_route_logic_and_respect_later_windows():
    for demo in (False,True):
        value=result(649,demo=demo,windows=[{'start':550,'end':650},{'start':900,'end':1000}])
        assert value['eta']['route']['kind']=='road'
        assert 'plan_late' in codes(value) and 'late' not in codes(value)
    assert result(539)['eta'] is None
    assert result(1080)['eta'] is None
    assert result(550,phase='unavailable')['target'] is None


def test_paused_real_device_keeps_stale_warning_but_off_does_not():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        gps.ingest(packet(point()))
        clock[0]+=95
        gps.ingest(packet(state='paused'))
        row=gps.snapshot()['rows'][0]
        assert 'stale' in codes(row) and 'paused' in codes(row)
        assert row['point_age_seconds']==95
        clock[0]+=1
        gps.ingest(packet(state='off'))
        assert 'stale' not in codes(gps.snapshot()['rows'][0])


def test_server_clock_refreshes_forecast_without_new_coordinate():
    clock=[NOW+9*60+55]
    with service(GpsService,clock=lambda:clock[0]) as gps:
        gps.ingest(packet(point(clock[0])))
        wait_for(lambda:gps.snapshot()['rows'][0]['analysis'] is not None)
        first=gps.snapshot()['rows'][0]
        assert first['analysis']['eta']['schedule']['now_minute']==549
        clock[0]+=10
        wait_for(lambda:(gps.snapshot()['rows'][0]['analysis'] or {}).get('eta',{}).get('schedule',{}).get('now_minute')==550)
        assert gps.snapshot()['rows'][0]['position']==first['position']
        clock[0]+=95
        stale=gps.snapshot()['rows'][0]
        assert 'stale' in codes(stale) and stale['analysis'] is None


def test_native_heartbeat_does_not_refresh_gps_or_add_history(native_server):
    server,_,_=native_server
    employee=Client(server);employee.login('worker')
    token=employee.request('/api/gps/mobile',{'action':'issue'})[1]['device_identifier']
    gps=server.gps.service;clock=[gps.clock()];gps.clock=lambda:clock[0]
    assert native_request(server,{'id':token,'lat':55.7,'lon':37.7,'accuracy':8,'timestamp':clock[0]})[0]==200
    clock[0]+=95
    assert native_request(server,{'id':token,'event':'heartbeat'})[0]==200
    row=next(r for r in gps.snapshot()['rows'] if r['engineer_id']=='e1')
    assert row['heartbeat_age_seconds']==0 and row['point_age_seconds']==95
    assert 'stale' in codes(row) and 'no_contact' not in codes(row)
    assert len(gps.history_page('e1')['samples'])==1
    assert native_request(server,{'id':token,'event':'heartbeat'},post=False)[0]==405
    assert native_request(server,{'id':token,'event':'heartbeat','lat':0})[0]==400


def test_heartbeat_without_fix_and_browser_lease_are_distinct():
    clock=[NOW]
    with service(clock=lambda:clock[0]) as gps:
        gps.native_heartbeat('e1','native-one')
        row=gps.snapshot()['rows'][0]
        assert row['position'] is None and 'stale' in codes(row)
    with service(clock=lambda:clock[0]) as gps:
        gps.ingest(packet(point()))
        clock[0]+=1
        assert gps.native_heartbeat('e1','native-two')['reason']=='awaiting_first_fix'
        assert gps.snapshot()['rows'][0]['source']=='browser'
