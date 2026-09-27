"""The provider is optional and must never hold up live autopilot batches."""
import json
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from conftest import engineer, job
from field_router.models import Point
from field_router.planning import make_plan
from field_router.replanning import ScenarioSession
from field_router.scenario_server import create_server
from field_router.travel import synthetic_travel


TRAVEL_CONFIG={
    'speed_kmh':{mode:60 for mode in ('car','walk','bicycle','public')},
    'distance_factor':{mode:1 for mode in ('car','walk','bicycle','public')},
    'boarding_minutes':{mode:0 for mode in ('car','walk','bicycle','public')},
}


def _session(make_scenario):
    scenario=make_scenario([job('a',start=600,end=700,duration=20)],
        [engineer('e1'),engineer('e2')]).model_copy(update={
            'metadata':{'geography':'synthetic_cartesian_km'},
            'travel_config':TRAVEL_CONFIG,
            'points':{'office':Point(x_km=0,y_km=0),'a':Point(x_km=1,y_km=0)}})
    plan=make_plan(scenario,synthetic_travel(scenario),{'e1':['a']},'test','valid')
    session=ScenarioSession(scenario,plan)
    session.set_planning_settings({'version':session.version,'mode':'autopilot',
                                   'every_n_requests':1,'every_n_minutes':None})
    return session


def _incoming(key):
    return {'id':key,'type':'new_request','anchor_id':'a','time':'09:00',
            'window_start':'12:00','window_end':'15:00','work_kind':'local',
            'duration_minutes':20,'router':0,'tv_box':0}


def test_slow_yandex_explanation_does_not_block_next_autopilot_batch(make_scenario,tmp_path):
    first_started=threading.Event()
    first_release=threading.Event()
    calls=[]
    class Explainer:
        def explain(self,batch,candidate_id):
            calls.append(candidate_id)
            if len(calls)==1:
                first_started.set()
                assert first_release.wait(8)
            return {'source':'yandexgpt','text':'Проверенные компромиссы выбранного плана.'}
        def recommend(self,batch):
            raise AssertionError('После применения нельзя повторно выбирать вариант')
    server=create_server(_session(make_scenario),tmp_path/'async-llm.json',
        port=0,time_limit=.03,disable_gps=True,explainer=Explainer())
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    opener=build_opener(ProxyHandler({}))
    root=f'http://127.0.0.1:{server.server_port}'
    def post(key):
        data=json.dumps({'request':_incoming(key)}).encode()
        with opener.open(Request(root+'/api/inbox/requests',data=data,
                headers={'Content-Type':'application/json'}),timeout=5) as response:
            return json.load(response)
    def state():
        with opener.open(root+'/api/inbox',timeout=5) as response:return json.load(response)
    try:
        post('first')
        assert first_started.wait(8)
        post('second')
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            snapshot=state()
            if sum(note['kind']=='auto_applied' for note in snapshot['dispatch_notifications'])>=2:
                break
            time.sleep(.05)
        assert sum(note['kind']=='auto_applied' for note in snapshot['dispatch_notifications'])==2
        assert len(calls)>=2
        first_release.set()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            snapshot=state()
            if sum(note['kind']=='explanation_ready' for note in snapshot['dispatch_notifications'])>=2:
                break
            time.sleep(.05)
        assert all(row['llm_status']=='ready' for row in snapshot['decision_log'])
        assert all('llm_explanation' in row for row in snapshot['decision_log'])
        assert all('Проверенные компромиссы' in row['plan_explanation'] for row in snapshot['decision_log'])
        assert sum(note['kind']=='explanation_ready' for note in snapshot['dispatch_notifications'])==2
        applied=[note for note in snapshot['dispatch_notifications'] if note['kind']=='auto_applied']
        assert all(note['llm_status']=='ready' and
                   'Проверенные компромиссы' in note['plan_explanation'] for note in applied)
    finally:
        first_release.set()
        server.shutdown();server.server_close();worker.join()


def test_unavailable_provider_is_visible_without_falsely_claiming_model_text(make_scenario,tmp_path):
    class Explainer:
        def explain(self,batch,candidate_id):
            return {'source':'template','notice':'Нет защищённого соединения с YandexGPT; показаны проверенные факты.'}
    server=create_server(_session(make_scenario),tmp_path/'offline-llm.json',
        port=0,time_limit=.03,disable_gps=True,explainer=Explainer())
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    opener=build_opener(ProxyHandler({}))
    root=f'http://127.0.0.1:{server.server_port}'
    try:
        data=json.dumps({'request':_incoming('offline')}).encode()
        with opener.open(Request(root+'/api/inbox/requests',data=data,
                headers={'Content-Type':'application/json'}),timeout=5):pass
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            with opener.open(root+'/api/inbox',timeout=5) as response:snapshot=json.load(response)
            applied=[note for note in snapshot['dispatch_notifications'] if note['kind']=='auto_applied']
            if applied and applied[-1].get('llm_status')=='unavailable':break
            time.sleep(.05)
        assert applied[-1]['llm_status']=='unavailable'
        assert 'YandexGPT' in applied[-1]['llm_notice']
        assert applied[-1]['plan_explanation']
        assert applied[-1]['llm_status']=='unavailable'
        assert snapshot['decision_log'][-1]['llm_status']=='unavailable'
        assert 'llm_explanation' not in snapshot['decision_log'][-1]
        assert not any(note['kind']=='explanation_ready' for note in snapshot['dispatch_notifications'])
    finally:
        server.shutdown();server.server_close();worker.join()
