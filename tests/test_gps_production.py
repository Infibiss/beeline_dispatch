"""Persistent device telemetry, offline delivery and scoped native credentials."""
from contextlib import closing
from copy import deepcopy
import hashlib
import http.client
import json
import sqlite3
import threading
import time
from urllib.parse import urlencode

import pytest

from field_router.auth import AccountStore
from field_router.gps.config import GpsConfig
from field_router.gps.contracts import GpsInputError
from field_router.gps.eta import RoutingEta, load_routing
from field_router.gps.mobile import DeviceRegistry, parse_position
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_auth import Client, PASSWORD
from test_gps import StoreOnly, context, point, packet, NOW
from test_replanning import demo


def test_history_survives_restart_but_demo_does_not(tmp_path):
    path=tmp_path/'private.gps.sqlite3'
    with closing(StoreOnly(context(),history_path=path,clock=lambda:NOW)) as gps:
        assert gps.ingest(packet(point(),sequence=4))['accepted']
        gps.ingest(packet(point(),tracker='demo'),channel='demo')
    with closing(StoreOnly(context(),history_path=path,clock=lambda:NOW+95)) as gps:
        row=gps.snapshot()['rows'][0]
        assert row['point_age_seconds']==95
        assert row['analysis'] is None
        assert row['position']==point()
        assert gps.snapshot('demo')['rows'][0]['position'] is None
        assert len(gps.history_page('e1')['samples'])==1
        assert gps.ingest(packet(sequence=3))['reason']=='old_message'


def test_offline_fix_is_archived_without_moving_current_position(tmp_path):
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:NOW)) as gps:
        gps.ingest_native('e1','native-test',point())
        old=point(NOW-3600,offset=.05)
        assert gps.ingest_native('e1','native-test',old)['reason']=='historical_only'
        gps.ingest_native('e1','native-test',old)
        assert gps.snapshot()['rows'][0]['position']==point()
        assert len(gps.history_page('e1')['samples'])==2
        assert gps.ingest_native('e1','native-test',point(NOW-700000))['reason']=='expired_history'
        assert len(gps.history_page('e1')['samples'])==2


def test_bad_accuracy_is_not_converted_to_precise_location(tmp_path):
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:NOW)) as gps:
        assert gps.ingest(packet(point(accuracy=20000)))['reason']=='low_accuracy'
        row=gps.snapshot()['rows'][0]
        assert row['position'] is None and row['analysis'] is None
        assert row['last_accuracy_m']==20000
        assert gps.history_page('e1')['samples'][0]['accepted'] is False


def test_native_handoff_waits_for_reliable_fix_and_retains_browser_on_failure(tmp_path, monkeypatch):
    clock=[NOW]
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:clock[0])) as gps:
        gps.ingest(packet(point(),sequence=1))
        assert gps.tracker_status('e1','native-new')['state']=='awaiting'
        clock[0]+=1
        gps.ingest_native('e1','native-new',point(clock[0],accuracy=20000))
        assert gps.tracker_status('e1','native-new')['state']=='low_accuracy'
        assert gps.snapshot()['rows'][0]['source']=='browser'
        assert gps.snapshot()['rows'][0]['position']==point()
        save=gps.history.save
        def broken(*args):raise sqlite3.OperationalError('disk full')
        monkeypatch.setattr(gps.history,'save',broken)
        clock[0]+=1
        with pytest.raises(GpsInputError):gps.ingest_native('e1','native-new',point(clock[0]))
        assert gps.snapshot()['rows'][0]['source']=='browser'
        monkeypatch.setattr(gps.history,'save',save)
        assert gps.ingest_native('e1','native-new',point(clock[0]))['accepted']
        assert gps.tracker_status('e1','native-new')['state']=='active'
        clock[0]+=1
        with pytest.raises(GpsInputError) as conflict:gps.ingest(packet(point(clock[0]),sequence=2))
        assert conflict.value.status==409
        assert gps.snapshot()['rows'][0]['source']=='native'
        clock[0]+=91
        assert gps.tracker_status('e1','native-new')['state']=='stale'


def test_imprecise_first_native_fix_does_not_block_browser_start(tmp_path):
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:NOW)) as gps:
        gps.ingest_native('e1','native-new',point(accuracy=20000))
        assert gps.tracker_status('e1','native-new')['state']=='low_accuracy'
        assert gps.ingest(packet(point(),sequence=1))['accepted']
        assert gps.snapshot()['rows'][0]['source']=='browser'


def test_failed_commit_returns_retry_and_rolls_back_live_state(tmp_path,monkeypatch):
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:NOW)) as gps:
        save=gps.history.save
        def broken(*args):raise sqlite3.OperationalError('disk full')
        monkeypatch.setattr(gps.history,'save',broken)
        with pytest.raises(GpsInputError) as err:gps.ingest(packet(point(),sequence=1))
        assert err.value.status==503
        assert gps.snapshot()['rows'][0]['position'] is None
        monkeypatch.setattr(gps.history,'save',save)
        assert gps.ingest(packet(point(),sequence=1))['accepted']
        assert gps.snapshot()['health']['storage_error'] is None


def test_expired_last_fix_is_not_exposed_by_snapshot(tmp_path):
    clock=[NOW]
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:clock[0])) as gps:
        gps.ingest(packet(point()))
        clock[0]+=604801
        assert gps.snapshot()['rows'][0]['position'] is None
        assert not gps.snapshot()['rows'][0]['analysis_pending']


def test_cursor_and_retention_are_bounded(tmp_path):
    clock=[NOW]
    with closing(StoreOnly(context(),history_path=tmp_path/'gps.sqlite3',clock=lambda:clock[0])) as gps:
        for i in range(4):
            gps.ingest_native('e1','native-test',point(NOW-1000-i))
        first=gps.history_page('e1',limit=2)
        second=gps.history_page('e1',limit=2,before=first['next_cursor'])
        assert first['next_cursor'] and second['next_cursor'] is None
        assert len({s['point']['measured_at_ms'] for s in first['samples']+second['samples']})==4
        clock[0]+=604801
        assert gps.history_page('e1')['samples']==[]
        with pytest.raises(GpsInputError):gps.history_page('e1',limit=999999)
        with pytest.raises(GpsInputError):gps.history_page('e1',before='nan:3')


@pytest.mark.parametrize('stamp',[str(int(NOW)),str(int(NOW*1000)),'2026-09-20T06:00:00Z'])
def test_native_protocol_time_and_unknown_precision(stamp):
    value=parse_position({'lat':'55.7','lon':'37.7','timestamp':stamp},NOW)
    assert value['measured_at_ms']==NOW*1000
    assert value['accuracy_m']==100000
    assert parse_position({'valid':'false'},NOW) is None
    with pytest.raises(GpsInputError):parse_position({'lat':'nan','lon':'0','timestamp':stamp},NOW)


def test_device_credentials_expire_rotate_revoke_and_follow_account_changes(tmp_path):
    clock=[NOW]
    accounts=AccountStore(tmp_path/'accounts.sqlite3')
    accounts.create_engineer('worker',PASSWORD,'east','e1')
    registry=DeviceRegistry(tmp_path/'devices.sqlite3',accounts,clock=lambda:clock[0])
    identity={'username':'worker','role':'engineer','region':'east','engineer_id':'e1'}
    first=registry.issue(identity)['device_identifier']
    assert registry.identify(first)['engineer']=='e1'
    assert first.encode() not in (tmp_path/'devices.sqlite3').read_bytes()
    second=registry.issue(identity)['device_identifier']
    with pytest.raises(GpsInputError):registry.identify(first)
    accounts.update('worker',password=PASSWORD+'new')
    with pytest.raises(GpsInputError):registry.identify(second)
    third=registry.issue(identity)['device_identifier']
    clock[0]+=43201
    with pytest.raises(GpsInputError):registry.identify(third)
    fourth=registry.issue(identity)['device_identifier']
    registry.revoke('worker')
    with pytest.raises(GpsInputError):registry.identify(fourth)


@pytest.fixture
def native_server(make_scenario,tmp_path,monkeypatch):
    scenario,plan=demo(make_scenario)
    accounts=AccountStore(tmp_path/'accounts.sqlite3');accounts.bootstrap(PASSWORD)
    session=ScenarioSession(scenario,plan)
    server=create_server(session,tmp_path/'state.json',port=0,accounts=accounts,
                         public_origin='https://gps-test.example')
    original_request=http.client.HTTPConnection.request
    def through_proxy(connection,method,url,body=None,headers=None,**kwargs):
        headers=dict(headers or {})
        if connection.port==server.server_port:
            headers.setdefault('Host','gps-test.example')
        return original_request(connection,method,url,body,headers,**kwargs)
    monkeypatch.setattr(http.client.HTTPConnection,'request',through_proxy)
    # The default region key is derived by RegionRegistry from this fixture.
    admin=Client(server)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    admin.login('admin')
    region=admin.request('/api/regions')[1]['current']
    accounts.create_engineer('worker',PASSWORD,region,'e1')
    yield server,accounts,region
    server.shutdown();server.server_close();thread.join()


def native_request(server,params,*,post=True,extra=None):
    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
    headers={'Content-Type':'application/x-www-form-urlencoded',**(extra or {})}
    raw=urlencode(params)
    conn.request('POST' if post else 'GET','/api/gps/native'+('' if post else '?'+raw),raw if post else None,headers)
    response=conn.getresponse();result=response.status,json.loads(response.read());conn.close()
    return result


def test_native_http_is_own_employee_only_and_cannot_read_plan(native_server):
    server,accounts,region=native_server
    employee=Client(server);assert employee.login('worker')[0]==200
    assert employee.request('/api/gps/mobile',{'action':'issue'},csrf=False)[0]==403
    status,record,_=employee.request('/api/gps/mobile',{'action':'issue'})
    assert status==200,record
    token=record['device_identifier']
    params={'id':token,'lat':55.7,'lon':37.7,'accuracy':10,'timestamp':time.time()}
    assert native_request(server,params)[0]==200
    assert native_request(server,params,post=False)[0]==200
    assert native_request(server,{**params,'id':'not-a-real-key'})[0]==401
    assert native_request(server,params,extra={'Origin':'https://evil.example'})[0]==403
    assert native_request(server,params,extra={'Host':f'127.0.0.1:{server.server_port}'})[0]==403
    assert Client(server).request('/api/state',origin=None)[0]==401
    assert employee.request('/api/gps/history?engineer_id=e1')[0]==403
    admin=Client(server);admin.login('admin')
    rows=admin.request('/api/gps')[1]['rows']
    assert next(r for r in rows if r['engineer_id']=='e1')['source']=='native'
    assert all(r['position'] is None for r in rows if r['engineer_id']!='e1')
    assert admin.request('/api/gps/history?engineer_id=e1')[1]['persistent']
    assert employee.request('/api/gps/mobile',{'action':'revoke'})[0]==200
    assert native_request(server,params)[0]==401


def test_preparing_native_access_is_not_reported_as_connected(native_server):
    server,accounts,region=native_server
    employee=Client(server);employee.login('worker')
    assert employee.request('/api/gps/telemetry',packet(point(time.time()),sequence=1))[0]==202
    status,issued,_=employee.request('/api/gps/mobile',{'action':'issue'})
    assert status==200
    connection=employee.request('/api/gps/mobile')[1]
    assert connection['paired'] and connection['connection']['state']=='awaiting'
    assert connection['source']=='browser' and connection['tracking_state']=='active'
    admin=Client(server);admin.login('admin')
    row=next(r for r in admin.request('/api/gps')[1]['rows'] if r['engineer_id']=='e1')
    assert row['native_connection']['state']=='awaiting'
    assert 'tracker' not in row['native_connection']
    assert native_request(server,{'id':issued['device_identifier'],'lat':55.71,'lon':37.7,
                                 'accuracy':8,'timestamp':time.time()+1})[0]==200
    assert employee.request('/api/gps/mobile')[1]['connection']['state']=='active'


def test_road_eta_uses_dynamic_origin_and_falls_back_on_failure():
    urls=[]
    def road(url):
        urls.append(url)
        return {'code':'Ok','waypoints':[{'distance':3},{'distance':2}],
                'routes':[{'duration':660,'distance':4200}]}
    route=RoutingEta(road).estimate(point(),{'lat':55.73,'lon':37.77},'car',{'car':'https://route.example'},'2026-09-20',540)
    assert route['kind']=='road' and route['minutes']==11 and route['metres']==4200
    assert '37.7000000,55.7100000' in urls[0]
    def failed(url):raise TimeoutError()
    fallback=RoutingEta(failed).estimate(point(),{'lat':55.73,'lon':37.77},'car',{'car':'https://route.example'},'2026-09-20',540)
    assert fallback['kind']=='distance_estimate'


def test_combined_eta_reuses_transit_graph_without_network():
    from field_router.multimodal import TransitNetwork
    data={'stations':[{'id':'a','lat':55.70,'lon':37.70,'line':'M','name':'A','order':0},
                      {'id':'b','lat':55.80,'lon':37.70,'line':'M','name':'B','order':1}]}
    eta=RoutingEta(lambda url:pytest.fail('unexpected external GPS disclosure'))
    eta.network=lambda date,minute:(TransitNetwork(data),False)
    route=eta.estimate({'lat':55.7001,'lon':37.7},{'lat':55.8001,'lon':37.7},'public',{},'2026-09-20',540)
    modes=[leg['mode'] for leg in route['legs']]
    assert route['kind']=='multimodal' and 'metro' in modes and 'walk' in modes and 'wait' in modes
    assert not route['road_access']


def test_routing_config_does_not_accept_secret_urls_or_public_plain_http(tmp_path):
    path=tmp_path/'routes.json'
    for url in ('http://public.example','https://user:secret@route.example','https://route.example?key=secret'):
        path.write_text(json.dumps({'car':url}))
        with pytest.raises(ValueError):load_routing(path)
    path.write_text(json.dumps({'walk':'http://127.0.0.1:5001'}))
    assert load_routing(path)['walk'].endswith(':5001')


def test_cloud_persistent_directory_is_not_recreated(tmp_path,monkeypatch):
    from field_router import cloud_demo
    root=tmp_path/'root'
    source=root/'data'/'demo-road';source.mkdir(parents=True)
    for region in ('Восток','Юго-восток','Югоцентр'):
        (source/(region+'.json')).write_text('{}')
    persistent=tmp_path/'volume';persistent.mkdir()
    marker=persistent/'keep.txt';marker.write_text('keep')
    monkeypatch.setenv('FIELD_ROUTER_ADMIN_PASSWORD',PASSWORD)
    monkeypatch.setenv('FIELD_ROUTER_PUBLIC_ORIGIN','https://gps.example')
    monkeypatch.setenv('FIELD_ROUTER_DATA_DIR',str(persistent))
    seen=[]
    monkeypatch.setattr(cloud_demo.scenario_server,'main',lambda args:seen.append(args))
    cloud_demo.run(root=root);cloud_demo.run(root=root)
    assert marker.read_text()=='keep'
    assert all(args[args.index('--state')+1]==str(persistent/'state.json') for args in seen)


def test_real_combined_network_fits_isolated_worker_contract():
    from field_router.gps.models import BaselineGpsModel
    from field_router.gps.service import GpsService
    engineer=deepcopy(context()['engineers']['e1']);engineer['transport']='public'
    engineer['tasks'][0]['point']={'lat':55.80,'lon':37.64}
    result=BaselineGpsModel().analyze({'points':[point()], 'engineer':engineer,
        'config':GpsConfig().public(),'now':NOW,'date':'2026-09-20','demo_minute':None,'routing':{}})
    assert result['eta']['route']['legs']
    assert GpsService._validate_result(result)
