import json
import threading
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
import pytest
from test_replanning import demo
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server


def test_region_state_and_drafts_are_isolated(make_scenario,tmp_path):
    s,p=demo(make_scenario)
    a=ScenarioSession(s,p);b=ScenarioSession(s,p)
    pa=tmp_path/'a.json';pb=tmp_path/'b.json'
    server=create_server(a,pa,regions={'south':(b,pb)},port=0,time_limit=.03)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    opener=build_opener(ProxyHandler({}));base=f'http://127.0.0.1:{server.server_port}'
    def call(path,payload=None):
        r=Request(base+path,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with opener.open(r) as response:return json.load(response)
    try:
        catalog=call('/regions/south/api/regions')
        assert catalog['current']=='south' and len(catalog['regions'])==2
        draft=call('/api/preview',{'version':1,'event':{'id':'x','type':'cancel','time':540,'request_id':'b'}})
        with pytest.raises(HTTPError):call('/regions/south/api/approve',{'version':1,'batch_id':draft['id'],'candidate_id':'stable'})
        assert not pb.exists()
        call('/regions/south/api/mode',{'version':1,'mode':'manual'})
        assert call('/api/state')['version']==1
        assert call('/regions/south/api/state')['version']==2
        assert ScenarioSession.restore(json.loads(pb.read_text())).mode()=='manual'
        assert not pa.exists()
        with opener.open(base+'/regions/south/') as r:html=r.read().decode()
        assert 'href="/regions/south/crew"' in html and 'id="region-select"' in html
        with opener.open(base+'/regions/south/crew') as r:html=r.read().decode()
        assert 'href="/regions/south/"' in html and 'src="/regions/south/crew.js"' in html
        with pytest.raises(HTTPError) as e:call('/regions/unknown/api/state')
        assert e.value.code==404
    finally:server.shutdown();server.server_close();thread.join()


def test_refuse_shared_region_state_file(make_scenario,tmp_path):
    s,p=demo(make_scenario);a=ScenarioSession(s,p)
    with pytest.raises(ValueError,match='отдельные'):
        create_server(a,tmp_path/'state.json',regions={'south':(a,tmp_path/'state.json')},port=0)


def test_restoration_rejects_wrong_region(tmp_path):
    from pathlib import Path
    from field_router.scenario_server import load_session
    source=Path('data/demo-road')
    saved=tmp_path/'day.json'
    session=load_session(source/'Восток.json',saved)
    saved.write_text(json.dumps(session.dump()))
    with pytest.raises(ValueError,match='другому региону'):
        load_session(source/'Югоцентр.json',saved)
