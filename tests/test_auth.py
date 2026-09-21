import http.client
import json
import threading

import pytest

from field_router.auth import AccountStore, AuthError
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from test_replanning import demo

PASSWORD = 'test-password-2026'


class Client:
    def __init__(self, server):
        self.port=server.server_port
        self.cookie=''
        self.csrf=''

    def request(self, path, payload=None, *, csrf=True, method=None, origin=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=15)
        headers={'Content-Type':'application/json','Cookie':self.cookie}
        if csrf: headers['X-CSRF-Token']=self.csrf
        if origin: headers['Origin']=origin
        connection.request(method or ('GET' if payload is None else 'POST'),path,
                           json.dumps(payload) if payload is not None else None,headers)
        response=connection.getresponse()
        raw=response.read()
        body=json.loads(raw) if 'application/json' in response.getheader('Content-Type','') else raw.decode()
        cookie=response.getheader('Set-Cookie')
        if cookie: self.cookie=cookie.split(';')[0]
        headers=dict(response.getheaders())
        status=response.status
        connection.close()
        return status,body,headers

    def login(self, username, password=PASSWORD):
        status,body,headers=self.request('/api/auth/login',{'username':username,'password':password})
        if status==200: self.csrf=body['csrf']
        return status,body,headers


@pytest.fixture
def secured(make_scenario,tmp_path):
    scenario,plan=demo(make_scenario)
    def region(label):
        return scenario.model_copy(update={'region':label,
            'engineers':[e.model_copy(update={'region':label}) for e in scenario.engineers],
            'requests':[r.model_copy(update={'region':label}) for r in scenario.requests]})
    east=region('Восток')
    south=region('Юго-восток')
    source=tmp_path/'south.json'
    source.write_text(json.dumps({'scenario':south.model_dump(mode='json'),'plans':[plan.model_dump(mode='json')]}),encoding='utf-8')
    sources={'east':{'label':'Восток','state':tmp_path/'state.json'},
             'south-east':{'label':'Юго-восток','state':tmp_path/'south-state.json','comparison':source}}
    accounts=AccountStore(tmp_path/'accounts.sqlite3')
    accounts.bootstrap(PASSWORD)
    accounts.create_engineer('worker1',PASSWORD,'east','e1')
    session=ScenarioSession(east,plan)
    server=create_server(session,tmp_path/'state.json',port=0,disable_gps=True,
                         accounts=accounts,region_sources=sources)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    yield server,accounts,session
    server.shutdown();server.server_close();thread.join()


def test_identity_cannot_cross_engineers_roles_or_regions(secured):
    server,_,session=secured
    anonymous=Client(server)
    assert anonymous.request('/api/state')[0]==401
    status,_,headers=anonymous.request('/engineer')
    assert status==303 and headers['Location']=='/login'
    assert anonymous.request('/login')[0]==200
    employee=Client(server)
    status,identity,headers=employee.login('worker1')
    assert status==200 and identity['redirect']=='/r/east/engineer'
    assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
    status,index,_=employee.request('/r/east/api/engineers')
    assert status==200 and [e['id'] for e in index['engineers']]==['e1']
    assert employee.request('/api/engineer?engineer_id=e1')[0]==200
    assert employee.request('/regions/east/api/engineer?engineer_id=e1')[0]==200
    assert employee.request('/regions/east/api/engineers')[1]['engineers'][0]['id']=='e1'
    for url in ('/api/engineer?engineer_id=e2','/r/south-east/api/engineers','/api/state',
                '/api/accounts','/api/crew','/dispatcher','/report','/crew','/accounts',
                '/api/gps','/api/gps/trace?engineer_id=e2&channel=device',
                '/regions/east/api/state','/regions/south-east/api/engineers',
                '/regions/east/api/engineer?engineer_id=e2'):
        assert employee.request(url)[0]==403,url
    for url,payload in (
        ('/api/gps/telemetry',{'engineer_id':'e2'}),
        ('/api/engineer/facts',{'engineer_id':'e2'}),
        ('/api/crew/apply',{}),('/api/preview',{}),('/api/gps/demo',{}),('/api/gps/replay',{}),
        ('/regions/east/api/gps/telemetry',{'engineer_id':'e2'}),
        ('/regions/east/api/preview',{})):
        assert employee.request(url,payload)[0]==403,url
    payload={'version':1,'engineer_id':'e1','status':'available','time':540,'location_id':'office'}
    assert employee.request('/api/engineer/facts',payload,csrf=False)[0]==403
    assert employee.request('/api/engineer/facts',payload,origin='https://foreign.example')[0]==403
    assert employee.request('/api/engineer/facts',payload)[0]==200
    admin=Client(server);assert admin.login('admin')[0]==200
    assert admin.request('/api/state')[1]['fact_journal'][0]['engineer_id']=='e1'
    assert admin.request('/api/accounts')[0]==200
    assert admin.request('/accounts')[0]==200
    assert employee.request('/api/auth/logout',{},method='POST')[0]==200
    assert employee.request('/api/engineers')[0]==401


def test_account_provision_reset_disable_and_removed_roster(secured):
    server,accounts,session=secured
    admin=Client(server);admin.login('admin')
    create={'action':'create','username':'second','password':PASSWORD,'region':'east','engineer_id':'e2'}
    assert admin.request('/api/accounts',{**create,'engineer_id':'ghost'})[0]==400
    assert admin.request('/api/accounts',create)[0]==200
    assert admin.request('/api/accounts',create)[0]==409
    employee=Client(server);assert employee.login('second')[0]==200
    assert admin.request('/api/accounts',{'action':'password','username':'second','password':PASSWORD+'new'})[0]==200
    assert employee.request('/api/auth/me')[0]==401
    assert employee.login('second')[0]==401
    assert employee.login('second',PASSWORD+'new')[0]==200
    assert admin.request('/api/accounts',{'action':'enabled','username':'second','enabled':False})[0]==200
    assert employee.request('/api/auth/me')[0]==401
    assert employee.login('second',PASSWORD+'new')[0]==401
    first=Client(server);assert first.login('worker1')[0]==200
    del session.initial_engineers['e1']
    assert first.request('/api/engineers')[0]==401
    assert first.login('worker1')[0]==403


def test_password_storage_expiration_restart_and_rate_limit(tmp_path):
    now=[100]
    path=tmp_path/'accounts.sqlite3'
    store=AccountStore(path,clock=lambda:now[0])
    assert store.bootstrap(PASSWORD)==PASSWORD
    assert store.bootstrap('ignored') is None
    assert PASSWORD.encode() not in path.read_bytes()
    assert 'digest' not in store.list()[0]
    token,_=store.login('admin',PASSWORD,'test')
    assert store.identify(token)['role']=='admin'
    now[0]+=43201
    assert store.identify(token) is None
    other=AccountStore(path)
    token,_=other.login('admin',PASSWORD,'test')
    store.update('admin',password=PASSWORD+'new',allow_admin=True)
    assert other.identify(token) is None
    for _ in range(10):
        with pytest.raises(AuthError) as error: store.login('unknown','wrong','attacker')
        assert error.value.status==401
    with pytest.raises(AuthError) as error: store.login('unknown','wrong','attacker')
    assert error.value.status==429
