"""Android-specific stop protocol on the real server; no phone location involved."""
import time
from test_auth import Client
from test_gps_production import native_server, native_request


def test_native_stop_revokes_own_key_and_keeps_history(native_server):
    server,_,_=native_server
    employee=Client(server);employee.login('worker')
    token=employee.request('/api/gps/mobile',{'action':'issue'})[1]['device_identifier']
    params={'id':token,'lat':55.7,'lon':37.7,'accuracy':8,'timestamp':time.time()}
    assert native_request(server,params)[0]==200
    assert native_request(server,{'id':token,'event':'stop'},post=False)[0]==405
    assert native_request(server,{'id':token,'event':'stop','lat':0})[0]==400
    assert native_request(server,{'id':token,'event':'unknown'})[0]==400
    assert native_request(server,{'id':token,'event':'stop'})==(200,{'ok':True,'stopped':True})
    assert native_request(server,params)[0]==401
    admin=Client(server);admin.login('admin')
    row=next(r for r in admin.request('/api/gps')[1]['rows'] if r['engineer_id']=='e1')
    assert row['tracking_state']=='off'
    assert len(admin.request('/api/gps/history?engineer_id=e1')[1]['samples'])==1
    assert not employee.request('/api/gps/mobile')[1]['paired']


def test_previous_phone_cannot_stop_new_phone(native_server):
    server,_,_=native_server
    employee=Client(server);employee.login('worker')
    old=employee.request('/api/gps/mobile',{'action':'issue'})[1]['device_identifier']
    new=employee.request('/api/gps/mobile',{'action':'issue'})[1]['device_identifier']
    assert native_request(server,{'id':old,'event':'stop'})[0]==401
    assert native_request(server,{'id':new,'lat':55.7,'lon':37.7,'accuracy':8,'timestamp':time.time()})[0]==200
    assert employee.request('/api/gps/mobile')[1]['connection']['state']=='active'


def test_engineer_receives_android_integration_script(native_server):
    server,_,_=native_server
    employee=Client(server);employee.login('worker')
    assert employee.request('/gps-android.js')[0]==200
    assert Client(server).request('/engineer-app.apk',origin=None)[0] in (302,303)
