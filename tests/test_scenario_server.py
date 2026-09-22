import json
import threading
from urllib.request import Request, build_opener, ProxyHandler
# Local HTTP contract tests must not inherit an external proxy.
urlopen=build_opener(ProxyHandler({})).open
from urllib.error import HTTPError
import pytest
from test_replanning import demo


def test_local_api_persists_approval_and_rejects_foreign_origin(make_scenario,tmp_path):
    from field_router.replanning import ScenarioSession
    from field_router.scenario_server import create_server
    s,p=demo(make_scenario);path=tmp_path/'state.json'
    server=create_server(ScenarioSession(s,p),path,port=0,time_limit=.03)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    def post(route,data,origin=None):
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        with urlopen(Request(url+route,data=json.dumps(data).encode(),headers=headers)) as r:return json.load(r)
    try:
        with pytest.raises(HTTPError) as error:post('/api/preview',{},'https://example.com')
        assert error.value.code==403
        batch=post('/api/preview',{'version':1,'event':{'id':'x','type':'cancel','time':540,'request_id':'b'}})
        assert not path.exists()
        state=post('/api/approve',{'version':1,'batch_id':batch['id'],'candidate_id':'stable'})
        assert state['version']==2
        assert json.loads(path.read_text())['version']==2
        with pytest.raises(HTTPError):post('/api/approve',{'version':1,'batch_id':batch['id'],'candidate_id':'stable'})
    finally:
        server.shutdown();server.server_close();thread.join()


@pytest.fixture
def unified_server(make_scenario, tmp_path):
    """A small session double isolates HTTP persistence and historical selection."""
    from field_router.scenario_server import create_server
    s, p = demo(make_scenario)

    class Session:
        version = 2
        scenario = s
        initial_engineers = {engineer.id: engineer for engineer in s.engineers}

        def state(self):
            return {'version': self.version, 'versions': [1, 2]}

        def dump(self):
            return self.state()

        def workbench(self, version=None):
            if version not in (None, 1, 2):
                raise ValueError('Неизвестная версия')
            scenario = s.model_dump(mode='json')
            scenario['region'] = 'История' if version == 1 else 'Текущий план'
            for item in scenario['requests'] + scenario['engineers']:
                item['region'] = scenario['region']
            return {'scenario': scenario, 'plans': [p.model_dump(mode='json')]}

        def record_fact(self, payload):
            if payload['version'] != self.version:
                raise ValueError('Конфликт версии')
            self.version += 1
            return self.state()

    path = tmp_path / 'state.json'
    server = create_server(Session(), path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', path
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_workbench_report_assets_and_csp(unified_server):
    url, _ = unified_server
    with urlopen(url + '/dispatcher?version=1') as response:
        html = response.read().decode()
        assert 'История' in html
        assert 'href="/report?version=1"' in html
        policy = response.headers['Content-Security-Policy']
        assert response.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
        assert "frame-ancestors 'self'" in policy
        assert "script-src 'self' 'unsafe-inline'" in policy
        assert 'https://tile.openstreetmap.org' in policy
    for route in ('/report?version=1', '/report.html?version=1'):
        with urlopen(url + route) as response:
            assert 'История' in response.read().decode()
    for route in ('/assets/leaflet.js', '/assets/leaflet.css', '/assets/images/marker-icon.png'):
        with urlopen(url + route) as response:
            assert response.read()
    with urlopen(url + '/') as response:
        assert "'unsafe-inline'" not in response.headers['Content-Security-Policy']
        assert response.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    for route in ('/assets/../scenarios.js', '/assets/%2e%2e/scenarios.js', '/assets/LEAFLET-LICENSE'):
        with pytest.raises(HTTPError) as error:
            urlopen(url + route)
        assert error.value.code == 404


@pytest.mark.parametrize('query', ['version=', 'version=0', 'version=-1', 'version=no', 'version=1&version=2', 'version=1&extra=x', 'version=99'])
def test_workbench_rejects_bad_versions(unified_server, query):
    url, _ = unified_server
    for route in ('/dispatcher?', '/report?'):
        with pytest.raises(HTTPError) as error:
            urlopen(url + route + query)
        assert error.value.code == 400


def test_fact_persistence_and_failed_write_preserves_published_state(unified_server, monkeypatch):
    from pathlib import Path
    url, path = unified_server

    def post(version, origin=None):
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = origin
        return urlopen(Request(url + '/api/facts', data=json.dumps({'version': version}).encode(), headers=headers))

    with pytest.raises(HTTPError) as error:
        post(2, 'https://example.com')
    assert error.value.code == 403
    with post(2) as response:
        assert json.load(response)['version'] == 3
    assert json.loads(path.read_text())['version'] == 3
    with pytest.raises(HTTPError) as error:
        post(2)
    assert error.value.code == 409

    def fail_replace(*args):
        raise OSError('disk unavailable')

    monkeypatch.setattr(Path, 'replace', fail_replace)
    with pytest.raises(HTTPError) as error:
        post(3)
    assert error.value.code == 500
    with urlopen(url + '/api/state') as response:
        assert json.load(response)['version'] == 3
    assert json.loads(path.read_text())['version'] == 3


def test_new_get_routes_reject_foreign_origin_and_host(unified_server):
    from http.client import HTTPConnection
    from urllib.parse import urlsplit
    url, _ = unified_server
    for route in ('/dispatcher', '/report', '/assets/leaflet.js'):
        for headers in ({'Origin': 'https://example.com'}, {'Host': 'evil.example'}):
            connection = HTTPConnection(urlsplit(url).netloc)
            try:
                connection.request('GET', route, headers=headers)
                response = connection.getresponse()
                assert response.status == 403, (route, headers)
                response.read()
            finally:
                connection.close()


def test_llm_explains_without_mutating_batch_and_stale_request_rejected(make_scenario,tmp_path):
    from field_router.replanning import ScenarioSession
    from field_router.scenario_server import create_server
    from field_router.llm import Explainer
    s,p=demo(make_scenario);session=ScenarioSession(s,p);calls=[]
    def send(body):
        calls.append(body)
        return {'output':[{'content':[{'type':'output_text','text':'{"candidate_id":"stable","points":["coverage","changes"]}'}]}]}
    server=create_server(session,tmp_path/'state.json',port=0,time_limit=.03,explainer=Explainer(send=send))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(route,data):
        with urlopen(Request(f'http://127.0.0.1:{server.server_port}'+route,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})) as r:return json.load(r)
    try:
        b=post('/api/preview',{'version':1,'event':{'id':'r','type':'replan','time':540}})
        assert b['llm_enabled']
        payload={'version':1,'batch_id':b['id'],'candidate_id':'stable'}
        assert post('/api/explain',payload)['source']=='yandexgpt'
        post('/api/explain',payload)
        assert len(calls)==1
        assert post('/api/recommend',{'version':1,'batch_id':b['id']})['candidate_id'] in ('sla','stable','cost')
        assert len(calls)==2
        # Approval still verifies the original fingerprint after explanation.
        assert post('/api/approve',payload)['version']==2
        with pytest.raises(HTTPError):post('/api/explain',payload)
        assert len(calls)==2
    finally:
        server.shutdown();server.server_close();thread.join()
